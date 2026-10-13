"""Untrusted-input regressions; synthetic streams only, no live CLI or accounts."""
import contextlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location(
    'security_workflow', Path(__file__).resolve().parents[1] / 'scripts/workflow.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
MODEL = 'gemini-synthetic-high'


def stream(error='', model=MODEL, status='ERROR', **result_fields):
    rows = [{'event': 'init', 'conversation_id': 'synthetic-session', 'init': {'model': model}},
            {'event': 'result', 'result': {'status': status, 'error': error,
             'response': 'synthetic response', 'conversation_id': 'synthetic-session', **result_fields}}]
    return '\n'.join(json.dumps(row) for row in rows)


def task():
    return {'task_id': 'security', 'model': MODEL, 'prompt': 'Synthetic bounded reasoning',
            'acceptance': 'Synthetic output', 'read_only': True, 'retry_safe': True}


class StreamSecurityTests(unittest.TestCase):
    def test_resource_exhausted_without_quota_evidence_cannot_rotate(self):
        result = w.classify(stream('RESOURCE_EXHAUSTED'), '', 1, MODEL)
        self.assertNotEqual(result['status'], 'QUOTA_EXHAUSTED')

    def test_capacity_and_overload_diagnostics_cannot_rotate(self):
        diagnostics = [
            'RESOURCE_EXHAUSTED: server overloaded',
            'RESOURCE_EXHAUSTED: capacity unavailable; please retry later',
            'RESOURCE_EXHAUSTED: quota exceeded because server capacity is unavailable',
            'quota exhausted: backend overloaded',
        ]
        for diagnostic in diagnostics:
            with self.subTest(diagnostic=diagnostic):
                result = w.classify(stream(diagnostic), '', 1, MODEL)
                self.assertNotEqual(result['status'], 'QUOTA_EXHAUSTED')

    def test_explicit_quota_exhaustion_still_permits_recovery(self):
        for diagnostic in ['RESOURCE_EXHAUSTED: quota exhausted', 'Insufficient quota']:
            with self.subTest(diagnostic=diagnostic):
                self.assertEqual(w.classify(stream(diagnostic), '', 1, MODEL)['status'], 'QUOTA_EXHAUSTED')

    def test_error_for_a_different_selected_model_cannot_rotate(self):
        result = w.classify(stream('quota exhausted', model='gemini-other'), '', 1, MODEL)
        self.assertNotEqual(result['status'], 'QUOTA_EXHAUSTED')

    def test_duplicate_initialization_cannot_authorize_rotation(self):
        data = json.dumps({'event': 'init', 'init': {'model': MODEL}}) + '\n' + stream('quota exhausted')
        self.assertNotEqual(w.classify(data, '', 1, MODEL)['status'], 'QUOTA_EXHAUSTED')

    def test_nonfinite_stream_numbers_are_protocol_errors(self):
        for value in [float('nan'), float('inf'), -float('inf')]:
            with self.subTest(value=repr(value)):
                result = w.classify(stream(status='SUCCESS', usage={'input_tokens': value}), '', 0, MODEL)
                self.assertEqual(result['status'], 'PROTOCOL_ERROR')
        # A normal JSON exponent may overflow Python's float parser too.
        data = stream(status='SUCCESS', usage={'input_tokens': 1})
        data = data.replace('"input_tokens": 1', '"input_tokens": 1e999')
        self.assertEqual(w.classify(data, '', 0, MODEL)['status'], 'PROTOCOL_ERROR')


class TaskSecurityTests(unittest.TestCase):
    def test_nonfinite_schema_numbers_are_rejected_before_cli_execution(self):
        for value in [float('nan'), float('inf')]:
            data = task()
            data['json_schema'] = {'type': 'number', 'minimum': value}
            with self.subTest(value=repr(value)), self.assertRaises(w.WorkflowError):
                w.validate_task(data)

    def test_wrong_effort_type_is_a_workflow_error(self):
        for value in [[], {}, None]:
            data = task()
            data['effort'] = value
            with self.subTest(value=value), self.assertRaises(w.WorkflowError):
                w.validate_task(data)


class RunFailurePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task_file = self.root / 'task.json'
        self.task_file.write_text(json.dumps(task()), encoding='utf-8')
        self.cli = self.root / 'synthetic.exe'
        self.cli.write_bytes(b'never executed')
        self.args = SimpleNamespace(task=str(self.task_file), cli=str(self.cli),
                                    workspace=str(self.root), audit_dir=str(self.root / 'audit'),
                                    preflight_dir='synthetic', timeout=30)

    def execute(self, completed, permission_error=None):
        identity = {'account_ref': 'acct-synthetic', 'manager_matches': True, 'manager_target': 'agy'}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(w, 'require_receipt', return_value={'synthetic': True}))
            stack.enter_context(mock.patch.object(w, 'Manager'))
            stack.enter_context(mock.patch.object(w, 'identify', return_value=identity))
            stack.enter_context(mock.patch.object(w, 'worker_lock', side_effect=contextlib.nullcontext))
            stack.enter_context(mock.patch.object(w, 'cli_processes', return_value=[]))
            runner = stack.enter_context(mock.patch.object(w.subprocess, 'run', return_value=completed))
            if permission_error:
                stack.enter_context(mock.patch.object(w, 'workspace_file_permissions', side_effect=permission_error))
            result = w.run_worker(self.args)
        return result, runner

    def state(self):
        return w.read_json(self.root / 'audit/task-security.json')

    def test_nonfinite_output_leaves_a_terminal_auditable_failure(self):
        completed = subprocess.CompletedProcess([], 0,
            stream(status='SUCCESS', usage={'input_tokens': float('nan')}).encode(), b'')
        result, runner = self.execute(completed)
        self.assertNotEqual(result['status'], 'SUCCESS')
        self.assertNotEqual(self.state()['attempts'][-1]['status'], 'RUNNING')
        self.assertTrue(Path(result['result_file']).is_file())
        self.assertEqual(runner.call_count, 1)

    def test_permission_setup_failure_is_recorded_and_never_starts_a_process(self):
        result, runner = self.execute(None, w.WorkflowError('Synthetic permission setup failure'))
        self.assertNotEqual(result['status'], 'SUCCESS')
        self.assertNotEqual(self.state()['attempts'][-1]['status'], 'RUNNING')
        self.assertTrue(Path(result['result_file']).is_file())
        runner.assert_not_called()

    def test_malformed_persistent_state_is_rejected_before_cli_execution(self):
        for attempts in [None, 'not-a-list', [None]]:
            with self.subTest(attempts=attempts):
                state_path = self.root / 'audit/task-security.json'
                state_path.parent.mkdir(exist_ok=True)
                state_path.write_text(json.dumps({'task_sha256': w.digest(self.task_file.read_bytes()),
                                                 'attempts': attempts}), encoding='utf-8')
                with self.assertRaises(w.WorkflowError):
                    _, runner = self.execute(None)


if __name__ == '__main__':
    unittest.main()
