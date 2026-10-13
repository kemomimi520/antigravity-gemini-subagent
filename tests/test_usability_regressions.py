"""Independent consumer regressions. All CLI/account/system interactions are mocked."""
import contextlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location(
    "usability_workflow", Path(__file__).resolve().parents[1] / "scripts/workflow.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

MODEL = "gemini-consumer-fixture-high"
CONVERSATION = "synthetic-consumer-session"


def task_contract():
    return {"task_id": "consumer", "model": MODEL, "effort": "high",
            "prompt": "Synthetic bounded task", "acceptance": "Verify fixture",
            "read_only": True, "retry_safe": True, "max_attempts": 3}


def stream(*, structured=None, tool=False, terminal=True):
    events = [{"event": "init", "conversation_id": CONVERSATION,
               "init": {"model": MODEL}}]
    if tool:
        events.append({"event": "step_update", "step_update": {
            "step_type": "tool", "step_index": 1, "state": "DONE",
            "tool_name": "read_file", "tool_info": {"output": "synthetic fixture"}}})
    if terminal:
        events.append({"event": "result", "result": {
            "status": "SUCCESS", "response": "synthetic response",
            "structured_output": structured, "conversation_id": CONVERSATION}})
    return "\n".join(json.dumps(event) for event in events).encode("utf-8")


class ConsumerRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task_path = self.root / "task.json"
        self.task_path.write_text(json.dumps(task_contract()), encoding="utf-8")
        self.args = SimpleNamespace(
            preflight_dir="synthetic", cli=sys.executable, workspace=str(self.root),
            task=str(self.task_path), audit_dir=str(self.root / "audit"), timeout=30,
            resume_review=None)

    def invoke(self, effect):
        identity = {"account_ref": "acct-synthetic", "manager_matches": True,
                    "manager_target": "agy"}
        with mock.patch.object(w, "require_receipt", return_value={"synthetic": True}), \
                mock.patch.object(w, "worker_lock", side_effect=contextlib.nullcontext), \
                mock.patch.object(w, "Manager"), \
                mock.patch.object(w, "identify", return_value=identity), \
                mock.patch.object(w, "cli_processes", return_value=[]), \
                mock.patch.object(w.subprocess, "run", side_effect=effect):
            return w.run_worker(self.args)

    def test_task_field_typos_are_rejected_before_defaults_apply(self):
        for typo, value in (("max_attemps", 8), ("allow_workspace_file", True),
                            ("json_shema", {"type": "object"})):
            with self.subTest(typo=typo):
                task = task_contract()
                task[typo] = value
                with self.assertRaises(w.WorkflowError):
                    w.validate_task(task)

    def test_native_schema_output_is_checked_for_required_field_types(self):
        task = task_contract()
        task["json_schema"] = {
            "type": "object", "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"], "additionalProperties": False}
        self.task_path.write_text(json.dumps(task), encoding="utf-8")
        completed = subprocess.CompletedProcess([], 0, stream(structured={"ok": "yes"}), b"")
        result = self.invoke([completed])
        self.assertEqual(result["status"], "PROTOCOL_ERROR")
        self.assertEqual(result["reason"], "invalid_structured_output")

    def test_timeout_preserves_partial_conversation_and_tool_evidence(self):
        partial = stream(tool=True, terminal=False)
        timeout = subprocess.TimeoutExpired("synthetic command", 30,
                                            output=partial, stderr=b"synthetic diagnostic")
        result = self.invoke([timeout])
        self.assertEqual(result["status"], "TIMEOUT")
        self.assertEqual(result["conversation_id"], CONVERSATION)
        self.assertEqual(result["tool_steps"], 1)
        self.assertEqual(result["stderr"], "synthetic diagnostic")
        self.assertEqual(result["stdout_sha256"], w.digest(partial))

    def test_permission_setup_error_never_leaves_a_running_attempt(self):
        with mock.patch.object(w, "workspace_file_permissions",
                               side_effect=w.WorkflowError("CLI_SETTINGS_CHANGED_BEFORE_GRANT")):
            try:
                self.invoke([])
            except w.WorkflowError:
                pass  # Raising is permitted if the failed attempt is finalized and audited.
        state = w.read_json(self.root / "audit/task-consumer.json")
        record = state["attempts"][-1]
        self.assertNotEqual(record["status"], "RUNNING")
        self.assertIn("CLI_SETTINGS_CHANGED_BEFORE_GRANT", json.dumps(record))
        result_path = self.root / "audit/consumer-attempt-1/result.json"
        self.assertTrue(result_path.is_file())


if __name__ == "__main__":
    unittest.main()
