"""Read-only diagnostics must preserve useful checks when one dependency fails."""
import contextlib
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('diagnostic_worker', Path(__file__).resolve().parents[1] / 'scripts/workflow.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


@unittest.skipUnless(w.os.name == 'nt', 'Full native workflow diagnostics support Windows')
class DoctorChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cli = self.root / 'synthetic.exe'
        self.cli.write_bytes(b'never executed')
        self.settings = self.root / 'settings.json'
        self.settings.write_text('{"permissions":{"allow":[]}}')
        self.paths = SimpleNamespace(cli_path=lambda explicit: self.cli,
            settings_path=lambda explicit: self.settings, manager_executable=lambda explicit: self.cli)
        self.args = SimpleNamespace(account_backend='tools', cli=None, settings_file=None, manager_cli=None, manager_dir=None, preflight_dir='synthetic')
        self.manager = SimpleNamespace(accounts=lambda **kw: [{'id': 'synthetic'}], inventory_source='local_cache')

    def inspect(self, receipt_error=None, manager_error=None, lock_error=None):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(w, 'sibling_module', return_value=self.paths))
            stack.enter_context(mock.patch.object(w, 'require_receipt', return_value={'synthetic': True}, side_effect=receipt_error))
            stack.enter_context(mock.patch.object(w, 'Manager', return_value=self.manager, side_effect=manager_error))
            stack.enter_context(mock.patch.object(w, 'identify', return_value={'status': 'IDENTIFIED', 'account_ref': 'acct-synthetic'}))
            stack.enter_context(mock.patch.object(w, 'worker_lock', side_effect=lock_error or contextlib.nullcontext))
            runner = stack.enter_context(mock.patch.object(w.subprocess, 'run'))
            result = w.doctor(self.args)
            runner.assert_not_called()
        return result

    def test_valid_environment_is_ready_without_cli_execution(self):
        self.assertEqual(self.inspect()['status'], 'READY')

    def test_missing_receipt_is_not_ready_and_keeps_identity_evidence(self):
        result = self.inspect(receipt_error=w.WorkflowError('PREFLIGHT_REQUIRED'))
        self.assertEqual(result['status'], 'NOT_READY')
        self.assertFalse(result['checks']['preflight']['ok'])
        self.assertTrue(result['checks']['identity']['ok'])

    def test_malformed_settings_keep_other_diagnostics(self):
        for data in (b'{', b'\xff', b'{"permissions":{"allow":true}}'):
            with self.subTest(data=data):
                self.settings.write_bytes(data)
                result = self.inspect()
                self.assertEqual(result['status'], 'NOT_READY')
                self.assertFalse(result['checks']['settings']['ok'])
                self.assertTrue(result['checks']['identity']['ok'])

    def test_missing_manager_is_explicit(self):
        result = self.inspect(manager_error=FileNotFoundError('synthetic inventory absent'))
        self.assertEqual(result['status'], 'NOT_READY')
        self.assertFalse(result['checks']['manager_or_identity']['ok'])

    def test_worker_busy_does_not_stop_other_checks(self):
        result = self.inspect(lock_error=w.WorkflowError('WORKER_BUSY'))
        self.assertEqual(result['status'], 'NOT_READY')
        self.assertTrue(result['checks']['identity']['ok'])
        self.assertFalse(result['checks']['worker_lock']['ok'])


if __name__ == '__main__':
    unittest.main()
