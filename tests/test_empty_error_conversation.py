"""Regression for live eligibility errors emitted before a conversation is created."""
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('w_empty_id', Path(__file__).resolve().parents[1] / 'scripts/workflow.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class EmptyErrorConversationTests(unittest.TestCase):
    def test_empty_terminal_id_retains_real_eligibility_error(self):
        event = {'event': 'result', 'result': {'conversation_id': '', 'status': 'ERROR', 'response': '',
                 'error': 'Eligibility check failed: Your current account is not eligible for Antigravity, because it is not currently available in your location.'}}
        result = w.classify(json.dumps(event), '', 1, 'gemini-3.8-flash-medium')
        self.assertEqual(result['status'], 'WORKER_ERROR')
        self.assertEqual(result['failure_kind'], 'AUTH_OR_ELIGIBILITY')
        self.assertIn('Eligibility check failed', result['error'])
        self.assertIsNone(result['conversation_id'])

    def test_empty_init_id_does_not_hide_pre_conversation_error(self):
        events = [{'event': 'init', 'conversation_id': '', 'init': {'model': 'gemini-3.8-flash-high'}},
                  {'event': 'result', 'result': {'conversation_id': '', 'status': 'ERROR', 'error': 'authentication required'}}]
        result = w.classify('\n'.join(json.dumps(x) for x in events), '', 1, 'gemini-3.8-flash-high')
        self.assertEqual(result['failure_kind'], 'AUTH_OR_ELIGIBILITY')
        self.assertEqual(result['status'], 'WORKER_ERROR')


if __name__ == '__main__':
    unittest.main()
