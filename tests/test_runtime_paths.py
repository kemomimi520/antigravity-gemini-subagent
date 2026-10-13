"""Native path discovery regressions; mock all file, environment and process probes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "paths_fixture", Path(__file__).resolve().parents[1] / "scripts/runtime_paths.py")
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class RuntimePathTests(unittest.TestCase):
    def setUp(self):
        self.root = (Path.cwd() / "synthetic-paths-only").resolve()

    def test_explicit_paths_override_environment(self):
        mappings = ((p.cli_path, "AGY_SUBAGENT_CLI"),
                    (p.manager_directory, "AGY_SUBAGENT_MANAGER_DIR"),
                    (p.settings_path, "AGY_SUBAGENT_SETTINGS"),
                    (p.manager_executable, "AGY_SUBAGENT_MANAGER_CLI"))
        for function, name in mappings:
            with self.subTest(name=name), mock.patch.dict(os.environ, {name: str(self.root / "environment")}), \
                    mock.patch.object(Path, "is_file", return_value=True):
                self.assertEqual(function(self.root / "explicit"), self.root / "explicit")

    def test_environment_and_user_paths_are_expanded(self):
        with mock.patch.dict(os.environ, {"AGY_SUBAGENT_SETTINGS": "$CONSUMER_PATH/settings.json",
                                         "CONSUMER_PATH": str(self.root)}):
            self.assertEqual(p.settings_path(), self.root / "settings.json")
        self.assertEqual(p.manager_directory("~/manager"), (Path.home() / "manager").resolve())

    def test_missing_explicit_cli_never_falls_back_to_path(self):
        with mock.patch.object(p.shutil, "which") as find:
            self.assertEqual(p.cli_path(self.root / "absent.exe"), self.root / "absent.exe")
            find.assert_not_called()

    def test_cli_existing_local_default_precedes_path(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}, clear=True), \
                mock.patch.object(Path, "is_file", return_value=True), \
                mock.patch.object(p.shutil, "which") as find:
            self.assertEqual(p.cli_path(), self.root / "agy/bin/agy.exe")
            find.assert_not_called()

    def test_cli_path_fallback_and_missing_diagnostic_candidate(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}, clear=True), \
                mock.patch.object(Path, "is_file", return_value=False), \
                mock.patch.object(p.shutil, "which", return_value=str(self.root / "path-agy.exe")):
            self.assertEqual(p.cli_path(), self.root / "path-agy.exe")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}, clear=True), \
                mock.patch.object(Path, "is_file", return_value=False), \
                mock.patch.object(p.shutil, "which", return_value=None):
            self.assertEqual(p.cli_path(), self.root / "agy/bin/agy.exe")

    def test_default_manager_directory_and_settings(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(Path, "home", return_value=self.root):
            self.assertEqual(p.manager_directory(), self.root / ".antigravity_tools")
            self.assertEqual(p.settings_path(), self.root / ".gemini/antigravity-cli/settings.json")

    def test_missing_explicit_manager_is_actionable_and_never_falls_back(self):
        with mock.patch.object(Path, "is_file", return_value=False), \
                mock.patch.object(p, "_running_manager_paths") as processes, \
                mock.patch.object(p.shutil, "which") as find:
            with self.assertRaisesRegex(FileNotFoundError, "AGY_SUBAGENT_MANAGER_CLI"):
                p.manager_executable(self.root / "absent.exe")
            processes.assert_not_called()
            find.assert_not_called()

    def test_unique_running_manager_is_preferred(self):
        running = self.root / "running/antigravity-tools.exe"
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(p, "_running_manager_paths", return_value=[running, running]), \
                mock.patch.object(Path, "is_file", return_value=True), \
                mock.patch.object(p.shutil, "which") as find:
            self.assertEqual(p.manager_executable(), running)
            find.assert_not_called()

    def test_multiple_running_installations_require_explicit_selection(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(p, "_running_manager_paths", return_value=[self.root / "a.exe", self.root / "b.exe"]):
            with self.assertRaisesRegex(ValueError, "Multiple.*running"):
                p.manager_executable()

    def test_manager_path_precedes_limited_install_candidates(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(p, "_running_manager_paths", return_value=[]), \
                mock.patch.object(Path, "is_file", return_value=True), \
                mock.patch.object(p.shutil, "which", return_value=str(self.root / "path-manager.exe")):
            self.assertEqual(p.manager_executable(), self.root / "path-manager.exe")

    def test_limited_common_install_directory_discovery(self):
        candidate = self.root / "Programs/Antigravity Tools/antigravity-tools.exe"
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}, clear=True), \
                mock.patch.object(p, "_running_manager_paths", return_value=[]), \
                mock.patch.object(p.shutil, "which", return_value=None), \
                mock.patch.object(Path, "is_file", side_effect=lambda: True):
            self.assertEqual(p.manager_executable(), candidate)

    def test_absent_manager_is_actionable(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(p, "_running_manager_paths", return_value=[]), \
                mock.patch.object(p.shutil, "which", return_value=None):
            with self.assertRaisesRegex(FileNotFoundError, "AGY_SUBAGENT_MANAGER_CLI"):
                p.manager_executable()

    @unittest.skipUnless(os.name == "nt", "Windows CIM entry point")
    def test_cim_discovery_is_hidden_and_exposes_no_command_line(self):
        path = self.root / "running.exe"
        completed = subprocess.CompletedProcess([], 0, json.dumps([str(path)]).encode(), b"")
        with mock.patch.object(p.subprocess, "run", return_value=completed) as run:
            self.assertEqual(p._running_manager_paths(), [path])
        script = run.call_args.args[0][-1]
        self.assertIn("ExecutablePath", script)
        self.assertNotIn("CommandLine", script)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)


if __name__ == "__main__":
    unittest.main()
