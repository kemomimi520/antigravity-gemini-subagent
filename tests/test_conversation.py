"""Conversation and reviewed quota checkpoints; all CLI/account calls are synthetic."""
import contextlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('w_conversation_tests', Path(__file__).resolve().parents[1] / 'scripts/workflow.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
MODEL = 'gemini-synthetic-high'
CONVERSATION = '8c598b25-c7db-46c2-b035-7247e5b6f074'


def completed(status='SUCCESS', conversation=CONVERSATION, tools=False, init_conversation=None):
    rows = [{'event': 'init', 'conversation_id': init_conversation or conversation, 'init': {'model': MODEL}}]
    if tools:
        rows.append({'event': 'step_update', 'step_update': {'step_type': 'tool', 'state': 'DONE',
                     'tool_info': {'name': 'write_file', 'output': 'synthetic completed effect'}}})
    result = {'status': status, 'response': 'synthetic response'}
    if conversation is not None:
        result['conversation_id'] = conversation
    if status == 'ERROR':
        result['error'] = 'RESOURCE_EXHAUSTED: quota exhausted in synthetic fixture'
    rows.append({'event': 'result', 'result': result})
    return subprocess.CompletedProcess([], 0 if status == 'SUCCESS' else 1,
                                       '\n'.join(json.dumps(x) for x in rows).encode(), b'')


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = {'task_id': 'conversation', 'model': MODEL, 'prompt': 'Synthetic bounded task',
                     'acceptance': 'Synthetic answer', 'read_only': True, 'retry_safe': True, 'max_attempts': 3}
        self.task_path = self.root / 'input.json'
        self.task_path.write_text(json.dumps(self.task))
        executable = self.root / 'synthetic.exe'
        executable.write_bytes(b'synthetic executable; never launched')
        self.args = SimpleNamespace(task=str(self.task_path), cli=str(executable), workspace=str(self.root),
                                    audit_dir=str(self.root / 'audit'), preflight_dir='synthetic', timeout=30)

    def execute(self, result, account='acct-new'):
        identity = {'account_ref': account, 'manager_matches': True, 'manager_target': 'agy'}
        with mock.patch.object(w, 'require_receipt', return_value={'synthetic': True}), \
                mock.patch.object(w, 'worker_lock', side_effect=contextlib.nullcontext), \
                mock.patch.object(w, 'Manager'), mock.patch.object(w, 'cli_processes', return_value=[]), \
                mock.patch.object(w, 'identify', return_value=identity), \
                mock.patch.object(w.subprocess, 'run', return_value=result) as runner:
            return w.run_worker(self.args), runner

    def state(self):
        return w.read_json(self.root / 'audit/task-conversation.json')

    def review(self):
        state = self.state()
        return {'task_id': self.task['task_id'], 'task_sha256': state['task_sha256'],
                'last_attempt_sha256': w.attempt_digest(state['attempts'][-1]), 'conversation_id': CONVERSATION,
                'safe_to_resume': True, 'completed_effects': ['Synthetic checkpoint already exists'],
                'verification': 'Independent synthetic file hash check', 'remaining_work': 'Complete only the remaining result'}

    def set_review(self, review):
        path = self.root / 'review.json'
        path.write_text(json.dumps(review))
        self.args.resume_review = str(path)

    def test_explicit_followup_uses_exact_id_and_model(self):
        self.task['conversation_id'] = CONVERSATION
        self.task_path.write_text(json.dumps(self.task))
        result, runner = self.execute(completed())
        self.assertEqual(result['status'], 'SUCCESS')
        argv = runner.call_args.args[0]
        self.assertEqual(argv[argv.index('--conversation') + 1], CONVERSATION)
        self.assertEqual(argv[argv.index('--model') + 1], MODEL)
        self.assertNotIn('--continue', argv)
        self.assertEqual(self.state()['attempts'][0]['resume_source'], 'task')

    def test_quota_retry_resumes_retained_conversation_on_new_account(self):
        first, _ = self.execute(completed('ERROR'), 'acct-old')
        second, runner = self.execute(completed())
        self.assertEqual(first['status'], 'QUOTA_EXHAUSTED')
        self.assertEqual(second['status'], 'SUCCESS')
        argv = runner.call_args.args[0]
        self.assertEqual(argv[argv.index('--conversation') + 1], CONVERSATION)
        self.assertIn('complete only remaining work', argv[argv.index('--print') + 1])
        self.assertEqual(self.state()['attempts'][1]['resume_source'], 'quota_attempt')

    def test_initial_call_starts_fresh_without_a_requested_id(self):
        result, runner = self.execute(completed())
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertNotIn('--conversation', runner.call_args.args[0])

    def test_cli_starting_different_conversation_is_rejected(self):
        self.task['conversation_id'] = CONVERSATION
        self.task_path.write_text(json.dumps(self.task))
        result, _ = self.execute(completed(conversation='different-context'))
        self.assertEqual(result['status'], 'PROTOCOL_ERROR')
        self.assertEqual(result['reason'], 'requested_conversation_not_resumed')

    def test_missing_requested_conversation_is_not_a_quota_retry(self):
        self.task['conversation_id'] = CONVERSATION
        self.task_path.write_text(json.dumps(self.task))
        result, _ = self.execute(completed('ERROR', conversation='different-context'))
        self.assertEqual(result['status'], 'PROTOCOL_ERROR')

    def test_partial_effects_stop_without_review(self):
        self.execute(completed('ERROR', tools=True), 'acct-old')
        with self.assertRaisesRegex(w.WorkflowError, 'PARTIAL_EFFECTS_REQUIRE_REVIEW'):
            self.execute(completed())

    def test_reviewed_partial_effects_resume_with_checkpoint(self):
        self.execute(completed('ERROR', tools=True), 'acct-old')
        self.set_review(self.review())
        result, runner = self.execute(completed())
        self.assertEqual(result['status'], 'SUCCESS')
        argv = runner.call_args.args[0]
        self.assertEqual(argv[argv.index('--conversation') + 1], CONVERSATION)
        self.assertIn('Synthetic checkpoint already exists', argv[argv.index('--print') + 1])
        self.assertEqual(self.state()['attempts'][1]['resume_review']['safe_to_resume'], True)

    def test_stale_checkpoint_review_is_rejected(self):
        self.execute(completed('ERROR', tools=True), 'acct-old')
        review = self.review()
        review['last_attempt_sha256'] = 'stale'
        self.set_review(review)
        with self.assertRaisesRegex(w.WorkflowError, 'REVIEW_INVALID_OR_STALE'):
            self.execute(completed())

    def test_unverified_checkpoint_review_is_rejected(self):
        self.execute(completed('ERROR', tools=True), 'acct-old')
        review = self.review()
        review['verification'] = ''
        self.set_review(review)
        with self.assertRaisesRegex(w.WorkflowError, 'REVIEW_INVALID_OR_STALE'):
            self.execute(completed())

    def test_review_does_not_override_failed_account_guard(self):
        self.execute(completed('ERROR', tools=True), 'acct-old')
        self.set_review(self.review())
        with self.assertRaisesRegex(w.WorkflowError, 'ALREADY_ATTEMPTED'):
            self.execute(completed(), 'acct-old')

    def test_review_does_not_override_non_quota_failure(self):
        self.execute(completed())
        self.set_review(self.review())
        with self.assertRaisesRegex(w.WorkflowError, 'NON_QUOTA_RETRY_REFUSED'):
            self.execute(completed())

    def test_workspace_change_is_rejected_before_cli(self):
        self.execute(completed('ERROR'), 'acct-old')
        other = self.root / 'other'
        other.mkdir()
        self.args.workspace = str(other)
        with self.assertRaisesRegex(w.WorkflowError, 'RETRY_WORKSPACE_CHANGED'):
            self.execute(completed())

    def test_review_without_failed_attempt_is_rejected(self):
        self.set_review({'safe_to_resume': True})
        with self.assertRaisesRegex(w.WorkflowError, 'WITHOUT_FAILED_ATTEMPT'):
            self.execute(completed())


class ConversationProtocolTests(unittest.TestCase):
    def test_init_id_is_retained_when_terminal_quota_omits_it(self):
        process = completed('ERROR', conversation=None, init_conversation=CONVERSATION)
        result = w.classify(process.stdout.decode(), '', process.returncode, MODEL)
        self.assertEqual(result['conversation_id'], CONVERSATION)
        self.assertEqual(result['status'], 'QUOTA_EXHAUSTED')

    def test_conflicting_init_and_terminal_id_is_rejected(self):
        process = completed(init_conversation='different-context')
        result = w.classify(process.stdout.decode(), '', process.returncode, MODEL)
        self.assertEqual(result['status'], 'PROTOCOL_ERROR')

    def test_invalid_conversation_task_values_are_rejected(self):
        task = {'task_id': 'test', 'model': MODEL, 'prompt': 'test', 'acceptance': 'test', 'read_only': True, 'retry_safe': True}
        for value in ('', '--continue', 'id\nother', None, False, 123):
            with self.subTest(value=value), self.assertRaisesRegex(w.WorkflowError, 'INVALID_CONVERSATION_ID'):
                w.validate_task({**task, 'conversation_id': value})

    def test_partial_effects_without_id_cannot_be_replayed_under_review(self):
        task = {'task_id': 'test', 'retry_safe': False}
        state = {'task_sha256': 'hash', 'attempts': [{'status': 'QUOTA_EXHAUSTED', 'tool_steps': 1, 'account_ref': 'old'}]}
        with self.assertRaisesRegex(w.WorkflowError, 'PARTIAL_EFFECTS_REQUIRE_CONVERSATION'):
            w.check_retry(task, state, 'hash', 'new', {'safe_to_resume': True})


if __name__ == '__main__':
    unittest.main()
