"""Synthetic regressions for live-observed CLI integration problems."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('workflow_compat', Path(__file__).resolve().parents[1] / 'scripts/workflow.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class CompatibilityTests(unittest.TestCase):
    def account(self):
        return {'id': 'synthetic', 'disabled': False, 'proxy_disabled': True,
                'validation_blocked': False, 'protected_models': [], 'live_limited_models': {},
                'quota': {'is_forbidden': False, 'last_updated': 2000000000,
                          'models': [{'name': 'gemini-test-high', 'percentage': 90}]},
                'token': {'access_token': 'synthetic-access', 'refresh_token': 'synthetic-refresh',
                          'expiry_timestamp': 2000000000}}

    def test_proxy_disabled_is_not_a_cli_eligibility_failure(self):
        result = w.rank_accounts([self.account()], 'gemini-test-high', now=2000000000)
        self.assertEqual(result['status'], 'CANDIDATE')

    def test_unknown_proxy_routing_flag_does_not_disable_cli(self):
        account = self.account()
        account.pop('proxy_disabled')
        self.assertEqual(w.rank_accounts([account], 'gemini-test-high', now=2000000000)['status'], 'CANDIDATE')

    def test_actual_disabled_account_still_stops(self):
        account = self.account()
        account['disabled'] = True
        self.assertEqual(w.rank_accounts([account], 'gemini-test-high', now=2000000000)['status'], 'NO_ELIGIBLE_ACCOUNT')

    def test_native_millisecond_expiry_matches_seconds(self):
        account = self.account()
        milliseconds = copy.deepcopy(account)
        milliseconds['token']['expiry_timestamp'] *= 1000
        self.assertEqual(w.build_cli_payload(account), w.build_cli_payload(milliseconds))

    def test_empty_credential_fields_are_rejected(self):
        for key in ('access_token', 'refresh_token'):
            with self.subTest(key=key):
                account = self.account()
                account['token'][key] = ''
                with self.assertRaisesRegex(w.WorkflowError, 'ACCOUNT_TOKEN_INVALID'):
                    w.build_cli_payload(account)

    def test_discovery_does_not_return_command_line_or_prompt(self):
        payload = [{'pid': 123, 'created_at': '2026-10-12T00:00:00Z', 'CommandLine': 'synthetic private prompt'}]
        completed = subprocess.CompletedProcess([], 0, json.dumps(payload).encode(), b'')
        with mock.patch.object(w.os, 'name', 'nt'), mock.patch.object(w.subprocess, 'run', return_value=completed):
            result = w.cli_processes()
        self.assertEqual(result, [{'pid': 123, 'created_at': '2026-10-12T00:00:00Z'}])

    def test_failed_or_invalid_process_snapshot_is_explicit(self):
        for stdout in (b'null', b'{}', b'not json', b'[{"pid":true,"created_at":"x"}]'):
            with self.subTest(stdout=stdout), mock.patch.object(w.os, 'name', 'nt'), mock.patch.object(w.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, stdout, b'')):
                with self.assertRaisesRegex(w.WorkflowError, 'CLI_PROCESS_DISCOVERY_FAILED'):
                    w.cli_processes()

    def test_live_unavailable_model_does_not_trigger_account_rotation(self):
        stdout = '\n'.join(json.dumps(x) for x in [
            {'event': 'init', 'init': {'model': 'gemini-test-high'}},
            {'event': 'result', 'result': {'status': 'ERROR', 'response': '', 'error': 'invalid model selection: unavailable'}}])
        result = w.classify(stdout, '', 1, 'gemini-test-high')
        self.assertEqual(result['status'], 'WORKER_ERROR')
        self.assertEqual(result['failure_kind'], 'NON_QUOTA_ERROR')


if __name__ == '__main__':
    unittest.main()
