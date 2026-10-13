"""Replay bounded real worker calls, using discovered Gemini models and synthetic data."""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import re
from pathlib import Path
import subprocess
import sys
import time

HELPER = Path(__file__).with_name('workflow.py')
spec = importlib.util.spec_from_file_location('matrix_workflow', HELPER)
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--audit-dir', required=True)
    parser.add_argument('--preflight-dir', required=True)
    parser.add_argument('--all-models', action='store_true')
    parser.add_argument('--include-tools', action='store_true')
    parser.add_argument('--tools-only', action='store_true', help='run only file/command cases, preserving earlier model evidence')
    parser.add_argument('--model')
    parser.add_argument('--cli', default=str(w.sibling_module('runtime_paths').cli_path()))
    parser.add_argument('--manager-dir')
    parser.add_argument('--account-backend', choices=('builtin', 'tools'), default='builtin')
    parser.add_argument('--manager-cli')
    parser.add_argument('--settings-file')
    parser.add_argument('--timeout', type=int, default=180)
    args = parser.parse_args()
    w.require_receipt(args.preflight_dir)
    root = Path(args.workspace).resolve()
    audit = Path(args.audit_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    audit.mkdir(parents=True, exist_ok=True)
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'cases': []}
    report_path = audit / 'live-matrix.json'

    def save():
        w.save_json(report_path, report)

    def invoke(command, extra):
        argv = [sys.executable, '-X', 'utf8', '-B', str(HELPER), command,
                '--audit-dir', str(audit), '--account-backend', args.account_backend, *extra]
        for name in ('manager_dir', 'manager_cli'):
            if getattr(args, name):
                argv += ['--' + name.replace('_', '-'), getattr(args, name)]
        if command in ('models', 'run'):
            argv += ['--cli', args.cli]
        if command == 'run' and args.settings_file:
            argv += ['--settings-file', args.settings_file]
        started = time.monotonic()
        completed = subprocess.run(argv, capture_output=True, timeout=args.timeout + 60,
                                   shell=False, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        stdout = completed.stdout.decode('utf-8', errors='replace')
        try:
            result = json.loads(stdout)
        except ValueError:
            result = {'status': 'HARNESS_PROTOCOL_ERROR', 'stderr': w.redact(completed.stderr.decode('utf-8', errors='replace'))}
        return result, completed.returncode, round(time.monotonic() - started, 3)

    models, code, seconds = invoke('models', ['--workspace', str(root), '--preflight-dir', args.preflight_dir])
    report['model_discovery'] = models
    if code != 0 or models['status'] != 'SUCCESS':
        report['status'] = 'DISCOVERY_FAILED'
        save()
        return 1
    discovered = models['models']
    selected = args.model or discovered[0]
    if selected not in discovered:
        raise ValueError('Explicit model is not in the actual model list')
    identities, _, _ = invoke('identify', [])
    report['identity_before'] = identities
    prefix = datetime.now(timezone.utc).strftime('matrix-%Y%m%dT%H%M%S')

    def case(name, model, prompt, checker, *, read_only=True, schema=None, denial_acceptable=False):
        case_root = root / name
        case_root.mkdir(exist_ok=True)
        task = {'task_id': prefix + '-' + name, 'model': model,
                'effort': model.rsplit('-', 1)[-1] if model.rsplit('-', 1)[-1] in ('low', 'medium', 'high') else 'medium',
                'prompt': prompt, 'acceptance': 'Complete only the exact bounded synthetic task; no unrelated tools, browsing, files or accounts.',
                'read_only': read_only, 'retry_safe': read_only, 'max_attempts': 1}
        if schema is not None:
            task['json_schema'] = schema
        if name in ('file-read', 'file-write'):
            task['allow_workspace_files'] = True
        task_file = audit / (task['task_id'] + '.json')
        w.save_json(task_file, task)
        result, returncode, elapsed = invoke('run', ['--task', str(task_file), '--workspace', str(case_root),
                                                  '--preflight-dir', args.preflight_dir, '--timeout', str(args.timeout)])
        diagnostics = json.dumps({key: result.get(key) for key in ('stderr', 'error', 'tool_errors', 'response')})
        accepted_denial = denial_acceptable and result.get('status') == 'INCOMPLETE_TOOLS' and bool(
            re.search(r'soft.denied|permission.{0,40}(?:denied|required)|requires? approval|not permitted|tool.{0,40}denied', diagnostics, re.I))
        valid = result.get('status') == 'SUCCESS' and returncode == 0 and checker(result, case_root)
        row = {'name': name, 'model': model, 'validation': 'PASS' if valid else ('EXPECTED_PERMISSION_DENIAL' if accepted_denial else 'FAIL'),
               'elapsed_seconds': elapsed, 'helper_returncode': returncode, 'result': result}
        report['cases'].append(row)
        save()
        print(json.dumps({k: row[k] for k in ('name', 'model', 'validation', 'elapsed_seconds')}) , flush=True)
        # Failure is preserved. Never rotate accounts or automatically rerun a task.
        return valid or accepted_denial

    all_selected = [] if args.tools_only else (discovered if args.all_models else [selected])
    proceed = True
    for index, model in enumerate(all_selected):
        token = 'AGY_MODEL_OK_' + str(index)
        if not case('model-' + str(index), model,
                    'Return exactly ' + token + '. Reason from this prompt only; do not use any tools or access files.',
                    lambda r, p, expected=token: r['response'].strip() == expected and r['tool_steps'] == 0):
            proceed = False
            break
    if proceed and not args.tools_only:
        proceed = case('unicode', selected,
            'Return exactly these two lines, preserving Chinese, quotes and the backslash. No tools:\n第一行：你好，子代理。\n第二行："A\\B"',
            lambda r, p: r['response'].strip() == '第一行：你好，子代理。\n第二行："A\\B"' and r['tool_steps'] == 0)
    if proceed and not args.tools_only:
        schema = {'type': 'object', 'properties': {'sum': {'type': 'integer'}, 'ok': {'type': 'boolean'},
                  'label': {'type': 'string'}, 'items': {'type': 'array', 'items': {'type': 'integer'}}},
                  'required': ['sum', 'ok', 'label', 'items'], 'additionalProperties': False}
        proceed = case('structured', selected,
            'Without tools return the object with sum=12, ok=true, label="中文\\路径", items=[2,4,6]. Use the native JSON schema.',
            lambda r, p: r.get('structured_output') == {'sum': 12, 'ok': True, 'label': '中文\\路径', 'items': [2,4,6]} and r['tool_steps'] == 0,
            schema=schema)
    if proceed and (args.include_tools or args.tools_only):
        case_root = root / 'file-read'
        case_root.mkdir(exist_ok=True)
        (case_root / 'input.json').write_text('{"numbers":[7,-3,11,2]}\n', encoding='utf-8')
        schema = {'type': 'object', 'properties': {'sum': {'type': 'integer'}}, 'required': ['sum'], 'additionalProperties': False}
        proceed = case('file-read', selected,
            'Use a file-read tool to read ONLY input.json in the current working directory. Return the integer sum of its numbers using the JSON schema. This is an authorized read. Do not write any file, run commands, browse, or read other files.',
            lambda r, p: r.get('structured_output') == {'sum':17} and r['tool_steps'] >= 1, schema=schema)
    if proceed and (args.include_tools or args.tools_only):
        proceed = case('file-write', selected,
            'Use the file-write tool to create ONLY result.txt as an ordinary workspace file (not a brain artifact) in the current working directory. Its exact UTF-8 content must be 子代理文件写入成功 followed by a single newline. Do not run commands, browse, read other files or change other files. Return DONE when complete.',
            lambda r, p: r['tool_steps'] >= 1 and (p/'result.txt').is_file() and (p/'result.txt').read_text(encoding='utf-8') == '子代理文件写入成功\n',
            read_only=False)
    if proceed and (args.include_tools or args.tools_only):
        proceed = case('command', selected,
            'Use the command tool to run exactly: powershell -NoProfile -Command "Write-Output AGY_COMMAND_OK_437". This exact harmless command is authorized. Do not run another command, browse or change files. Report its output; if the tool is denied, report the denial accurately and stop.',
            lambda r, p: 'AGY_COMMAND_OK_437' in r['response'] and r['tool_steps'] >= 1 and 'run_command' in r['tool_names'],
            read_only=False, denial_acceptable=True)
    after, _, _ = invoke('identify', [])
    report.update(identity_after=after, ended_at=datetime.now(timezone.utc).isoformat(),
                  status='PASS' if proceed else 'STOPPED_ON_FAILURE')
    save()
    print(json.dumps({'status': report['status'], 'case_count': len(report['cases']), 'report': str(report_path)}), flush=True)
    return 0 if proceed else 1


if __name__ == '__main__':
    raise SystemExit(main())
