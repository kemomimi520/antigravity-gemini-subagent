"""Real helper/CLI continuation acceptance with a bounded native switch and return."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
from types import SimpleNamespace

import workflow as w


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('model', 'workspace', 'audit-dir', 'preflight-dir'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--account-ref', help='existing alternative account for the native switch')
    parser.add_argument('--cli', default=str(w.sibling_module('runtime_paths').cli_path()))
    parser.add_argument('--manager-dir')
    parser.add_argument('--account-backend', choices=('builtin', 'tools'), default='builtin')
    parser.add_argument('--manager-cli')
    parser.add_argument('--settings-file')
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--start-manager', action='store_true')
    args = parser.parse_args()
    receipt = w.require_receipt(args.preflight_dir)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + secrets.token_hex(2)
    audit = Path(args.audit_dir).resolve() / ('conversation-' + stamp)
    job = Path(args.workspace).resolve() / ('conversation-' + stamp)
    audit.mkdir(parents=True)
    job.mkdir(parents=True)
    manager = w.account_service(args)
    baseline = w.identify(manager)
    index_path = manager.directory / 'accounts.json'
    baseline_index = w.read_json(index_path)
    settings = w.sibling_module('runtime_paths').settings_path(args.settings_file)
    settings_before = settings.read_bytes() if settings.is_file() else None
    inventory = manager.accounts(allow_local=True)
    target = args.account_ref or w.account_ref(baseline_index.get('current_account_id'))
    if args.account_ref and target == baseline['account_ref']:
        raise w.WorkflowError('EXPLICIT_ALTERNATIVE_ACCOUNT_REQUIRED')
    if not args.account_ref and target == baseline['account_ref']:
        target = next((w.account_ref(item['id']) for item in inventory
                       if w.account_ref(item['id']) != baseline['account_ref'] and item.get('disabled') is False
                       and not item.get('validation_blocked')), None)
    if not target or target == baseline['account_ref']:
        raise w.WorkflowError('NO_ALTERNATIVE_ACCOUNT_FOR_CONTINUATION_TEST')
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'preflight': receipt,
              'baseline_identity': baseline, 'other_cli_sessions': w.cli_processes(), 'cases': [],
              'quota_trigger': 'none: explicit authorized continuation acceptance'}
    payload = {'marker': 'CTX_' + secrets.token_hex(12), 'numbers': [17, 29, 43], 'label': '换号上下文保留'}
    checkpoint = job / 'checkpoint.json'
    output = job / 'remaining-output.json'
    report['expected_context'] = payload
    switched = False
    unknown = False

    def save():
        w.save_json(audit / 'live-conversation.json', report)

    def add(name, result, valid):
        report['cases'].append({'name': name, 'validation': 'PASS' if valid else 'FAIL', 'result': result})
        save()
        print(json.dumps({'name': name, 'validation': 'PASS' if valid else 'FAIL', 'status': result.get('status'),
                          'conversation_id': result.get('conversation_id')}, ensure_ascii=True), flush=True)
        if not valid:
            raise w.WorkflowError('CONVERSATION_ACCEPTANCE_FAILED: ' + name)

    def call(name, prompt, conversation=None, files=False, schema=None):
        task = {'task_id': 'conv-' + stamp + '-' + name, 'model': args.model,
                'prompt': prompt, 'acceptance': 'Complete only the requested synthetic continuity turn.',
                'read_only': not files, 'retry_safe': True, 'max_attempts': 1}
        if conversation:
            task['conversation_id'] = conversation
        if files:
            task['allow_workspace_files'] = True
        if schema:
            task['json_schema'] = schema
        task_path = audit / ('input-' + name + '.json')
        w.save_json(task_path, task)
        return w.run_worker(SimpleNamespace(account_backend=args.account_backend, task=str(task_path), cli=args.cli, workspace=str(job),
                            audit_dir=str(audit), preflight_dir=args.preflight_dir, timeout=args.timeout,
                            manager_dir=args.manager_dir, settings_file=args.settings_file))

    def recall_ok(result, conversation):
        try:
            value = result.get('structured_output') or json.loads(result.get('response', '').strip())
        except (ValueError, TypeError):
            return False
        return (result['status'] == 'SUCCESS' and value == payload and result.get('conversation_id') == conversation
                and result.get('identity_stable') and result.get('tool_steps') == 0)

    try:
        seed = call('seed', 'Remember this synthetic object for later turns: ' + json.dumps(payload, ensure_ascii=False)
                    + '. Write it as UTF-8 JSON to ' + checkpoint.as_posix()
                    + '. This is the only authorized file write. Return exactly CONTEXT_SAVED.', files=True)
        conversation = seed.get('conversation_id')
        valid = (seed['status'] == 'SUCCESS' and seed.get('response', '').strip() == 'CONTEXT_SAVED'
                 and w.valid_conversation_id(conversation) and seed.get('tool_steps', 0) > 0 and seed.get('identity_stable')
                 and checkpoint.is_file() and json.loads(checkpoint.read_text(encoding='utf-8-sig')) == payload)
        add('seed-with-completed-file-effect', seed, valid)
        checkpoint_hash = w.digest(checkpoint.read_bytes())
        checkpoint_mtime = checkpoint.stat().st_mtime_ns
        recall = 'Return the exact remembered object from the first turn as plain JSON without Markdown. Do not use tools or files. Return CONTEXT_MISSING if it is unavailable.'
        same = call('same', recall, conversation)
        add('same-account-resume', same, recall_ok(same, conversation))
        distractor = call('other', 'Remember DISTRACTOR_ONLY in this separate conversation. Return exactly DISTRACTOR_SAVED. Do not use tools or files.')
        add('separate-latest-conversation', distractor, distractor['status'] == 'SUCCESS' and distractor.get('conversation_id') != conversation)
        with w.worker_lock(), w.manager_session(manager, start=args.start_manager, executable=args.manager_cli, preflight_dir=args.preflight_dir, mode='desktop'):
            result = w.update_cli_target(manager, target)
        switched = True
        add('native-switch', result, result['status'] == 'SWITCHED')
        schema = {'type': 'object', 'properties': {'marker': {'type': 'string'}, 'numbers': {'type': 'array', 'items': {'type': 'integer'}},
                  'label': {'type': 'string'}}, 'required': ['marker', 'numbers', 'label'], 'additionalProperties': False}
        cross = call('cross', recall, conversation, schema=schema)
        add('cross-account-context-and-schema', cross, recall_ok(cross, conversation) and cross.get('account_ref') == target)
        remaining = call('remaining', 'Write the object remembered from the first turn as UTF-8 JSON to ' + output.as_posix()
                         + '. Read this new file back to verify it. The previous checkpoint is already verified and must remain unchanged. '
                         + 'This is the only authorized write. Return exactly REMAINING_WORK_OK.', conversation, files=True)
        valid = (remaining['status'] == 'SUCCESS' and remaining.get('response', '').strip() == 'REMAINING_WORK_OK'
                 and remaining.get('conversation_id') == conversation and remaining.get('tool_steps', 0) > 0
                 and remaining.get('identity_stable') and remaining.get('account_ref') == target and output.is_file()
                 and json.loads(output.read_text(encoding='utf-8-sig')) == payload
                 and w.digest(checkpoint.read_bytes()) == checkpoint_hash and checkpoint.stat().st_mtime_ns == checkpoint_mtime)
        add('cross-account-remaining-tools-without-repeating-effect', remaining, valid)
        report['worker_passed'] = True
    except Exception as error:
        report['error'] = w.redact(str(error))
        unknown = 'outcome_unknown' in str(error).lower() or 'switch_busy' in str(error).lower()
        report['status'] = 'UNKNOWN_STOP' if unknown else 'FAILED'
        save()
        print(json.dumps({'status': report['status'], 'error': report['error']}), flush=True)
    finally:
        if switched and not unknown:
            try:
                with w.worker_lock(), w.manager_session(manager, start=args.start_manager, executable=args.manager_cli, preflight_dir=args.preflight_dir, mode='desktop'):
                    current = w.identify(manager)
                    if current['account_ref'] not in (target, baseline['account_ref']):
                        raise w.WorkflowError('CONCURRENT_IDENTITY_CHANGED_PRESERVED')
                    returned = w.update_cli_target(manager, baseline['account_ref'])
                    add('native-return-to-baseline-account', returned, returned['status'] == 'SWITCHED')
                    owned = index_path.read_bytes()
                    index = json.loads(owned.decode('utf-8-sig'))
                    if w.account_ref(index.get('current_account_id')) != baseline['account_ref'] or index.get('current_target_ide') != 'agy':
                        raise w.WorkflowError('CONCURRENT_MANAGER_SELECTION_PRESERVED')
                    for key in ('current_account_id', 'current_target_ide'):
                        if key in baseline_index:
                            index[key] = baseline_index[key]
                        else:
                            index.pop(key, None)
                    if index_path.read_bytes() != owned:
                        raise w.WorkflowError('CONCURRENT_MANAGER_INDEX_PRESERVED')
                    w.atomic_write(index_path, w.json_bytes(index))
                    after = w.identify(manager)
                    valid = all(after[key] == baseline[key] for key in ('account_ref', 'manager_matches', 'manager_target'))
                    add('restore-original-manager-selection', after, valid)
                    report['baseline_identity_restored'] = valid
            except Exception as error:
                report['restore_error'] = w.redact(str(error))
                report['status'] = 'RESTORE_INCOMPLETE'
        settings_after = settings.read_bytes() if settings.is_file() else None
        report['settings_bytes_restored'] = settings_after == settings_before
        if report.get('status') not in ('UNKNOWN_STOP', 'FAILED', 'RESTORE_INCOMPLETE'):
            report['status'] = 'PASS' if report.get('worker_passed') and report.get('baseline_identity_restored') and report['settings_bytes_restored'] else 'PARTIAL'
        report['ended_at'] = datetime.now(timezone.utc).isoformat()
        save()
        print(json.dumps({'status': report['status'], 'baseline_identity_restored': report.get('baseline_identity_restored'),
                          'settings_bytes_restored': report['settings_bytes_restored'], 'report_path': str(audit / 'live-conversation.json')}), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
