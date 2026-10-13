"""Regression checks for protocol attribution, operation recovery and durable evidence."""
import contextlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('hardening_worker', Path(__file__).resolve().parents[1] / 'scripts/workflow.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
MODEL = 'gemini-synthetic-medium'


def stream(status='SUCCESS', steps=(), structured=None):
    events = [{'event': 'init', 'conversation_id': 'synthetic-context', 'init': {'model': MODEL}}]
    events += [{'event': 'step_update', 'step_update': step} for step in steps]
    events.append({'event': 'result', 'result': {'status': status, 'response': 'synthetic result',
                   'conversation_id': 'synthetic-context', 'structured_output': structured,
                   'error': 'quota exhausted' if status == 'ERROR' else ''}})
    return '\n'.join(json.dumps(event) for event in events).encode()


class ProtocolHardening(unittest.TestCase):
    def calls(self, target, parameters=True):
        first = {'step_type': 'tool', 'step_index': 1, 'state': 'DONE', 'tool_name': 'read_file',
                 'tool_info': {'name': 'read_file', 'error': {'type': 'invalid_args'}}}
        last = {'step_type': 'tool', 'step_index': 2, 'state': 'DONE', 'tool_name': 'read_file',
                'tool_info': {'name': 'read_file', 'output': 'synthetic contents'}}
        if parameters:
            first['tool_info']['parameters'] = {'AbsolutePath': '/synthetic/a.txt', 'EndLine': 0}
            last['tool_info']['parameters'] = {'AbsolutePath': target, 'EndLine': 5}
        return [first, last]

    def test_only_same_file_target_can_recover_a_failed_tool(self):
        same = w.classify(stream(steps=self.calls('/synthetic/a.txt')).decode(), '', 0, MODEL)
        other = w.classify(stream(steps=self.calls('/synthetic/b.txt')).decode(), '', 0, MODEL)
        self.assertEqual(same['status'], 'SUCCESS')
        self.assertTrue(same['tool_errors_recovered'])
        self.assertEqual(other['status'], 'INCOMPLETE_TOOLS')

    def test_missing_tool_arguments_cannot_prove_recovery(self):
        self.assertEqual(w.classify(stream(steps=self.calls('', False)).decode(), '', 0, MODEL)['status'], 'INCOMPLETE_TOOLS')

    def test_step_context_mismatch_stops_before_acceptance(self):
        step = {'step_type': 'tool', 'conversation_id': 'another-context', 'tool_info': {'name': 'read_file'}}
        self.assertEqual(w.classify(stream(steps=[step]).decode(), '', 0, MODEL)['status'], 'PROTOCOL_ERROR')

    def test_duplicate_json_fields_are_not_silently_replaced(self):
        with self.assertRaises(w.WorkflowError):
            w.strict_json('{"model":"first","model":"second"}')

    def test_attempt_budget_can_cover_all_existing_accounts(self):
        w.validate_task({'task_id': 'eight', 'model': MODEL, 'prompt': 'synthetic', 'acceptance': 'synthetic',
                         'read_only': True, 'retry_safe': True, 'max_attempts': 8})

    def test_omitted_effort_respects_model_suffix(self):
        self.assertEqual(w.task_effort({'model': MODEL}), 'medium')
        self.assertEqual(w.task_effort({'model': MODEL, 'effort': 'high'}), 'high')


class DurableEvidence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.job = self.root / 'job@example.test'
        self.job.mkdir()
        self.task = {'task_id': 'durable', 'model': MODEL, 'prompt': 'synthetic', 'acceptance': 'synthetic',
                     'read_only': True, 'retry_safe': True, 'max_attempts': 8}
        self.task_file = self.root / 'task.json'
        self.write_task()
        exe = self.root / 'synthetic.exe'
        exe.write_bytes(b'not executable; mocked')
        self.args = SimpleNamespace(task=str(self.task_file), cli=str(exe), workspace=str(self.job),
                                    audit_dir=str(self.root / 'audit'), preflight_dir='synthetic', timeout=30)

    def write_task(self):
        self.task_file.write_text(json.dumps(self.task), encoding='utf-8')

    def execute(self, output, account='acct-first', identity_error=None):
        identity = {'account_ref': account, 'manager_matches': True, 'manager_target': 'agy'}
        with mock.patch.object(w, 'require_receipt', return_value={'synthetic': True}), \
                mock.patch.object(w, 'Manager'), mock.patch.object(w, 'identify', return_value=identity,
                    side_effect=[identity, identity_error] if identity_error is not None else None), \
                mock.patch.object(w, 'worker_lock', side_effect=contextlib.nullcontext), \
                mock.patch.object(w, 'cli_processes', return_value=[]), \
                mock.patch.object(w.subprocess, 'run', side_effect=output):
            return w.run_worker(self.args)

    def test_redacted_workspace_path_does_not_break_same_workspace_retry(self):
        first = self.execute([subprocess.CompletedProcess([], 1, stream('ERROR'), b'')])
        self.assertEqual(first['status'], 'QUOTA_EXHAUSTED')
        second = self.execute([subprocess.CompletedProcess([], 0, stream(), b'')], 'acct-second')
        self.assertEqual(second['status'], 'SUCCESS')

    def test_redaction_cannot_return_schema_invalid_payload_as_success(self):
        self.task['json_schema'] = {'type': 'object', 'properties': {'password': {'type': 'integer'}}, 'required': ['password']}
        self.write_task()
        result = self.execute([subprocess.CompletedProcess([], 0, stream(structured={'password': 7}), b'')])
        self.assertEqual(result['status'], 'OUTPUT_REDACTED')
        self.assertTrue(result['structured_output_redacted'])

    def test_lost_account_file_after_response_preserves_terminal_evidence(self):
        result = self.execute([subprocess.CompletedProcess([], 0, stream(), b'')],
                              identity_error=FileNotFoundError('synthetic account file missing'))
        self.assertEqual(result['status'], 'IDENTITY_UNVERIFIABLE_AFTER_RUN')
        self.assertEqual(result['worker_status'], 'SUCCESS')
        state = w.read_json(self.root / 'audit/task-durable.json')
        self.assertEqual(state['attempts'][-1]['status'], 'IDENTITY_UNVERIFIABLE_AFTER_RUN')

    def test_failed_log_redaction_keeps_terminal_state_and_reports_risk(self):
        actual = w.atomic_write
        def write(path, data):
            if Path(path).name == 'cli.log':
                raise PermissionError('synthetic log ownership problem')
            actual(path, data)
        def run(*args, **kwargs):
            log = self.root / 'audit/durable-attempt-1/cli.log'
            log.write_text('synthetic log', encoding='utf-8')
            return subprocess.CompletedProcess([], 0, stream(), b'')
        with mock.patch.object(w, 'atomic_write', side_effect=write):
            result = self.execute(run)
        self.assertEqual(result['status'], 'AUDIT_CLEANUP_FAILED')
        self.assertEqual(result['exit_code'], 29)
        state = w.read_json(self.root / 'audit/task-durable.json')
        self.assertEqual(state['attempts'][-1]['status'], 'AUDIT_CLEANUP_FAILED')


class IdentityInputChecks(unittest.TestCase):
    def test_malformed_index_or_token_is_a_clear_workflow_error(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as name:
            root = Path(name)
            (root / 'accounts').mkdir()
            manager = SimpleNamespace(directory=root)
            for index in (None, {'accounts': None}, {'accounts': [None]}, {'accounts': [{'id': 'synthetic'}]}):
                with self.subTest(index=index):
                    (root / 'accounts.json').write_text(json.dumps(index))
                    (root / 'accounts/synthetic.json').write_text('{"token":null}')
                    with self.assertRaises(w.WorkflowError):
                        w.identify(manager, reader=lambda: 'synthetic-refresh')


if __name__ == '__main__':
    unittest.main()
