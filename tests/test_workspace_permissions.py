"""Synthetic scoped-grant tests; never touch the user's CLI settings."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('w_permissions', Path(__file__).resolve().parents[1] / 'scripts/workflow.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class WorkspacePermissionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'settings.json'
        self.original = b'{ "permissions": {"allow": ["command(git)"], "deny": ["write_file(secret)"], "ask": ["mcp(*)"]}, "other": true }\r\n'
        self.path.write_bytes(self.original)

    def test_read_only_grant_preserves_deny_ask_and_restores_exact_bytes(self):
        with w.workspace_file_permissions(self.root, True, True, self.path) as evidence:
            settings = w.read_json(self.path)
            self.assertEqual(settings['permissions']['deny'], ['write_file(secret)'])
            self.assertEqual(settings['permissions']['ask'], ['mcp(*)'])
            self.assertTrue(settings['other'])
            self.assertEqual(settings['permissions']['allow'][-1], 'read_file(' + self.root.as_posix() + ')')
        self.assertEqual(evidence['status'], 'restored')
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_write_grant_is_scoped_and_restored_after_process_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'synthetic timeout'):
            with w.workspace_file_permissions(self.root, False, True, self.path):
                self.assertEqual(w.read_json(self.path)['permissions']['allow'][-1], 'write_file(' + self.root.as_posix() + ')')
                raise RuntimeError('synthetic timeout')
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_concurrent_preferences_are_preserved_with_visible_warning(self):
        concurrent = b'{"concurrent_user_edit": true}\n'
        with w.workspace_file_permissions(self.root, False, True, self.path) as evidence:
            self.path.write_bytes(concurrent)
        self.assertEqual(self.path.read_bytes(), concurrent)
        self.assertEqual(evidence['warning'], 'WORKSPACE_PERMISSION_CLEANUP_CONCURRENT_CHANGE')

    def test_cli_formatting_rewrite_with_same_settings_is_restored(self):
        workspace = self.root / '中文 path'
        workspace.mkdir()
        with w.workspace_file_permissions(workspace, False, True, self.path) as evidence:
            settings = w.read_json(self.path)
            self.path.write_text(json.dumps(settings, ensure_ascii=False, indent=4), encoding='utf-8')
        self.assertEqual(evidence['status'], 'restored')
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_actual_boolean_to_numeric_change_is_preserved(self):
        with w.workspace_file_permissions(self.root, False, True, self.path) as evidence:
            settings = w.read_json(self.path)
            settings['other'] = 1
            self.path.write_bytes(w.json_bytes(settings))
        self.assertEqual(evidence['status'], 'concurrent_change_preserved')
        self.assertIs(type(w.read_json(self.path)['other']), int)

    def test_disabled_grant_does_not_read_or_modify_settings(self):
        self.path.write_bytes(b'not JSON')
        with w.workspace_file_permissions(self.root, False, False, self.path) as evidence:
            self.assertEqual(evidence['status'], 'not_requested')
        self.assertEqual(self.path.read_bytes(), b'not JSON')

    def test_preexisting_rule_is_not_removed(self):
        settings = json.loads(self.original)
        settings['permissions']['allow'].append('write_file(' + self.root.as_posix() + ')')
        self.path.write_bytes(w.json_bytes(settings))
        original = self.path.read_bytes()
        with w.workspace_file_permissions(self.root, False, True, self.path) as evidence:
            self.assertEqual(evidence['status'], 'already_allowed')
        self.assertEqual(self.path.read_bytes(), original)

    def test_missing_settings_use_normal_cli_defaults_without_creating_a_file(self):
        missing = self.root / 'absent.json'
        with w.workspace_file_permissions(self.root, False, True, missing) as evidence:
            self.assertEqual(evidence['status'], 'settings_absent_using_cli_defaults')
        self.assertFalse(missing.exists())

    def test_invalid_permission_list_is_preserved(self):
        malformed = b'{"permissions":{"allow":"wrong type"}}'
        self.path.write_bytes(malformed)
        with self.assertRaisesRegex(w.WorkflowError, 'INVALID_CLI_PERMISSIONS'):
            with w.workspace_file_permissions(self.root, False, True, self.path):
                self.fail('must not invoke worker')
        self.assertEqual(self.path.read_bytes(), malformed)
