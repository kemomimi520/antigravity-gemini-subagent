"""Synthetic contract tests. No real CLI call, account refresh or credential write."""

import copy
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location("workflow", Path(__file__).resolve().parents[1] / "scripts/workflow.py")
workflow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workflow)
NOW = 2000000000
MODEL = "gemini-test-high"


def account(identifier="one", percentage=90, weekly=0.8, short=0.9):
    return {"id": identifier, "email": identifier + "@example.test", "disabled": False,
            "proxy_disabled": False, "validation_blocked": False, "is_current": False,
            "protected_models": [], "live_limited_models": {},
            "quota": {"is_forbidden": False, "last_updated": NOW,
                      "models": [{"name": MODEL, "percentage": percentage}],
                      "quota_groups": [{"display_name": "Gemini Models", "buckets": [
                          {"bucket_id": "gemini-weekly", "remaining_fraction": weekly},
                          {"bucket_id": "gemini-5h", "remaining_fraction": short}]}]}}


def stream(status="SUCCESS", response="worker output", error=None, model=MODEL, tool=None):
    rows = [{"event": "init", "init": {"model": model}}]
    if tool:
        rows.append({"event": "step_update", "step_update": tool})
    rows.append({"event": "result", "result": {"status": status, "response": response, "error": error or ""}})
    return "\n".join(json.dumps(row) for row in rows)


class RankingTests(unittest.TestCase):
    def rank(self, items, **kwargs):
        return workflow.rank_accounts(items, MODEL, now=NOW, **kwargs)

    def test_minimum_window_beats_misleading_model_percentage(self):
        misleading = account("misleading", 100, 0.05, 1)
        better = account("better", 80, 0.8, 0.8)
        result = self.rank([misleading, better])
        self.assertEqual(result["selected"], workflow.account_ref("better"))
        self.assertEqual(result["accounts"][1]["score_percent"], 5)

    def test_zero_weekly_or_short_window_disqualifies(self):
        for field in ("weekly", "short"):
            with self.subTest(field=field):
                self.assertIsNone(self.rank([account(**{field: 0})])["selected"])

    def test_stale_future_and_missing_timestamp_disqualify(self):
        for timestamp in (NOW - 301, NOW + 31, None, float("nan"), "yesterday"):
            with self.subTest(timestamp=timestamp):
                item = account()
                item["quota"]["last_updated"] = timestamp
                self.assertIsNone(self.rank([item])["selected"])

    def test_partial_refresh_requires_per_account_freshness(self):
        stale = account("stale")
        stale["quota"]["last_updated"] = NOW - 1
        fresh = account("fresh")
        result = self.rank([stale, fresh], refreshed_after=NOW)
        self.assertEqual(result["selected"], workflow.account_ref("fresh"))
        self.assertIn("not_refreshed_this_round", result["accounts"][1]["reasons"])

    def test_flags_unknown_and_blocked_are_not_eligible(self):
        for flag in ("disabled", "validation_blocked"):
            for value in (True, None, "false"):
                with self.subTest(flag=flag, value=value):
                    item = account()
                    item[flag] = value
                    self.assertIsNone(self.rank([item])["selected"])
        item = account()
        item["quota"]["is_forbidden"] = True
        self.assertIsNone(self.rank([item])["selected"])

    def test_protected_or_limited_model_is_not_eligible(self):
        for key, value in (("protected_models", [MODEL]), ("live_limited_models", {MODEL: {"until": NOW + 60}})):
            item = account()
            item[key] = value
            self.assertIsNone(self.rank([item])["selected"])

    def test_real_empty_live_limit_mapping_is_eligible(self):
        self.assertIsNotNone(self.rank([account()])["selected"])

    def test_expired_live_limit_can_be_reconsidered_with_fresh_quota(self):
        item = account()
        item["live_limited_models"] = {MODEL: {"until": NOW - 1}}
        self.assertIsNotNone(self.rank([item])["selected"])

    def test_malformed_live_limit_is_unknown(self):
        for value in ([], {MODEL: None}, {MODEL: {"until": "later"}}, {MODEL: {"until": 0}}):
            item = account()
            item["live_limited_models"] = value
            self.assertIsNone(self.rank([item])["selected"])

    def test_bad_model_percentage_is_not_coerced(self):
        for value in (None, "100", True, -1, 101, float("inf"), float("nan")):
            with self.subTest(value=value):
                self.assertIsNone(self.rank([account(percentage=value)])["selected"])

    def test_bad_bucket_fraction_is_not_coerced(self):
        for value in (None, "1.0", True, -1, 2, float("inf"), float("nan")):
            with self.subTest(value=value):
                self.assertIsNone(self.rank([account(weekly=value)])["selected"])

    def test_missing_duplicate_and_wrong_model_are_not_guessed(self):
        for models in ([], [{"name": "gemini-other", "percentage": 100}],
                       [{"name": MODEL, "percentage": 90}, {"name": MODEL, "percentage": 80}]):
            item = account()
            item["quota"]["models"] = models
            self.assertIsNone(self.rank([item])["selected"])

    def test_current_cli_preferred_only_on_equal_score(self):
        current = account("current", 80, 0.8, 0.8)
        other = account("other", 80, 0.8, 0.8)
        result = self.rank([other, current], current_ref=workflow.account_ref("current"))
        self.assertEqual(result["selected"], workflow.account_ref("current"))
        other = account("other", 100, 1, 1)
        self.assertEqual(self.rank([current, other], current_ref=workflow.account_ref("current"))["selected"], workflow.account_ref("other"))

    def test_all_excluded_returns_no_candidate(self):
        result = self.rank([account()], exclude=[workflow.account_ref("one")])
        self.assertEqual(result["status"], "NO_ELIGIBLE_ACCOUNT")
        self.assertIsNone(result["selected"])

    def test_unrelated_vendor_buckets_do_not_affect_gemini(self):
        item = account()
        item["quota"]["quota_groups"].append({"display_name": "Claude", "buckets": [{"bucket_id": "3p-weekly", "remaining_fraction": 0}]})
        self.assertEqual(self.rank([item])["accounts"][0]["score_percent"], 80)

    def test_missing_gemini_group_is_unknown_when_groups_present(self):
        item = account()
        item["quota"]["quota_groups"] = [{"display_name": "Claude", "buckets": []}]
        self.assertIsNone(self.rank([item])["selected"])

    def test_inventory_ranking_is_deterministic(self):
        items = [account("a"), account("b"), account("c")]
        self.assertEqual(self.rank(items), self.rank(list(reversed(items))))


class ResultTests(unittest.TestCase):
    def classify(self, data, stderr="", code=0):
        return workflow.classify(data, stderr, code, MODEL)

    def test_valid_model_and_nonempty_terminal_response(self):
        self.assertEqual(self.classify(stream())["exit_code"], 0)

    def test_quota_error_allows_recovery(self):
        result = self.classify(stream(status="ERROR", response="", error="RESOURCE_EXHAUSTED: quota exceeded"), code=1)
        self.assertEqual(result["status"], "QUOTA_EXHAUSTED")
        self.assertEqual(result["exit_code"], 20)

    def test_successful_quota_discussion_does_not_switch(self):
        self.assertEqual(self.classify(stream(response="RESOURCE_EXHAUSTED means quota exceeded"))["status"], "SUCCESS")

    def test_auth_capacity_generic_429_do_not_rotate(self):
        for message in ("HTTP 429 too many requests", "HTTP 503 capacity unavailable", "401 quota exceeded",
                        "403 RESOURCE_EXHAUSTED", "invalid_grant: quota exhausted", "authentication required", "network disconnected"):
            with self.subTest(message=message):
                result = self.classify(stream(status="ERROR", error=message), code=1)
                self.assertEqual(result["status"], "WORKER_ERROR")

    def test_nonzero_with_success_is_not_accepted(self):
        self.assertNotEqual(self.classify(stream(), code=1)["exit_code"], 0)

    def test_missing_empty_duplicate_or_malformed_result_fails(self):
        for value in ("", "not json", "[]", '{}', stream(response=""), stream() + "\n" + stream()):
            with self.subTest(value=value):
                self.assertEqual(self.classify(value)["status"], "PROTOCOL_ERROR")

    def test_wrong_or_missing_model_evidence_fails(self):
        for model in (None, "gemini-other", "claude-other"):
            with self.subTest(model=model):
                self.assertEqual(self.classify(stream(model=model))["status"], "PROTOCOL_ERROR")

    def test_permission_denial_is_incomplete_even_exit_zero(self):
        self.assertEqual(self.classify(stream(), "run_command soft-denied: permission required")["status"], "INCOMPLETE_TOOLS")

    def test_ignored_execution_mode_is_not_accepted(self):
        self.assertEqual(self.classify(stream(), "warning: --mode plan has no effect while slash command expansion is disabled.")["status"], "INCOMPLETE_POLICY")

    def test_failed_tool_is_incomplete(self):
        tool = {"step_type": "tool", "tool_name": "read_file", "tool_info": {"name": "read_file", "error": {"type": "denied"}}}
        self.assertEqual(self.classify(stream(tool=tool))["status"], "INCOMPLETE_TOOLS")

    def test_recovered_tool_error_is_retained_as_a_warning(self):
        failed = {"step_type": "tool", "state": "DONE", "step_index": 1, "tool_name": "write_file",
                  "tool_info": {"name": "write_file", "parameters": {"TargetFile": "C:/synthetic/output.txt"}, "error": {"type": "invalid_args"}}}
        completed = {"step_type": "tool", "state": "DONE", "step_index": 2, "tool_name": "write_file",
                     "tool_info": {"name": "write_file", "parameters": {"TargetFile": "C:/synthetic/output.txt"}, "output": "created"}}
        rows = [json.loads(line) for line in stream(tool=failed).splitlines()]
        rows.insert(-1, {"event": "step_update", "step_update": completed})
        data = '\n'.join(json.dumps(row) for row in rows)
        result = self.classify(data)
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertTrue(result['tool_errors_recovered'])
        self.assertEqual(len(result['tool_errors']), 1)
        self.assertIn('TOOL_ERROR_FOLLOWED_BY_SUCCESS_REVIEW_OUTPUT', result['warnings'])
        completed['state'] = 'ACTIVE'
        rows[-2]['step_update'] = completed
        self.assertEqual(self.classify('\n'.join(json.dumps(row) for row in rows))['status'], 'INCOMPLETE_TOOLS')

    def test_another_tool_success_does_not_hide_an_unresolved_error(self):
        failed = {"step_type": "tool", "state": "DONE", "step_index": 1, "tool_name": "write_file",
                  "tool_info": {"name": "write_file", "error": {"type": "invalid_args"}}}
        rows = [json.loads(line) for line in stream(tool=failed).splitlines()]
        rows.insert(-1, {"event": "step_update", "step_update": {"step_type": "tool", "state": "DONE", "step_index": 2,
                           "tool_name": "read_file", "tool_info": {"name": "read_file", "output": "read"}}})
        self.assertEqual(self.classify('\n'.join(json.dumps(row) for row in rows))['status'], 'INCOMPLETE_TOOLS')

    def test_tool_effects_recorded_on_quota_error(self):
        tool = {"step_type": "tool", "tool_name": "write_file", "tool_info": {"name": "write_file"}}
        result = self.classify(stream(status="ERROR", error="quota exhausted", tool=tool), code=1)
        self.assertEqual(result["tool_steps"], 1)

    def test_malformed_tool_payload_is_a_protocol_failure(self):
        for value in ("invalid", ["invalid"], None):
            tool = {"step_type": "tool", "tool_info": value}
            self.assertEqual(self.classify(stream(tool=tool))["status"], "PROTOCOL_ERROR")

    def test_events_after_terminal_result_fail(self):
        data = stream() + '\n{"event":"step_update","step_update":{}}'
        self.assertEqual(self.classify(data)["status"], "PROTOCOL_ERROR")


    def test_eligibility_failure_does_not_rotate_even_with_quota_diagnostic(self):
        result = workflow.classify(stream(status="ERROR", response="", error="Eligibility check failed: not eligible for Antigravity. Quota exhausted diagnostic."), "", 1, MODEL)
        self.assertEqual(result["status"], "WORKER_ERROR")
        self.assertEqual(result["failure_kind"], "AUTH_OR_ELIGIBILITY")
        self.assertNotEqual(result["exit_code"], 20)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.task = {"task_id": "synthetic", "model": MODEL, "prompt": "Synthetic only", "acceptance": "Test",
                     "read_only": True, "retry_safe": True, "max_attempts": 3}
        self.state = {"task_sha256": "same", "attempts": [{"status": "QUOTA_EXHAUSTED", "account_ref": "acct-old", "tool_steps": 0}]}

    def test_new_account_quota_retry_is_permitted(self):
        workflow.check_retry(self.task, self.state, "same", "acct-new")

    def test_changed_task_rejected(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "TASK_CHANGED"):
            workflow.check_retry(self.task, self.state, "different", "acct-new")

    def test_same_failed_account_rejected(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "ALREADY_ATTEMPTED"):
            workflow.check_retry(self.task, self.state, "same", "acct-old")

    def test_partial_effects_and_unsafe_tasks_stop(self):
        self.state["attempts"][0]["tool_steps"] = 1
        with self.assertRaisesRegex(workflow.WorkflowError, "PARTIAL_EFFECTS"):
            workflow.check_retry(self.task, self.state, "same", "acct-new")
        self.state["attempts"][0]["tool_steps"] = 0
        self.task["retry_safe"] = False
        with self.assertRaisesRegex(workflow.WorkflowError, "PARTIAL_EFFECTS"):
            workflow.check_retry(self.task, self.state, "same", "acct-new")

    def test_cancel_timeout_success_and_running_are_not_retried(self):
        for status in ("TIMEOUT", "CANCELED", "SUCCESS", "RUNNING", "WORKER_ERROR"):
            self.state["attempts"][0]["status"] = status
            with self.assertRaisesRegex(workflow.WorkflowError, "NON_QUOTA_RETRY"):
                workflow.check_retry(self.task, self.state, "same", "acct-new")

    def test_attempt_budget_enforced(self):
        self.state["attempts"] *= 3
        with self.assertRaisesRegex(workflow.WorkflowError, "ATTEMPT_LIMIT"):
            workflow.check_retry(self.task, self.state, "same", "acct-new")

    def test_model_and_effort_are_explicit(self):
        workflow.validate_task(self.task)
        for key, value in (("model", "claude-sonnet"), ("effort", "max"), ("read_only", "true"), ("max_attempts", 0)):
            task = copy.deepcopy(self.task)
            task[key] = value
            with self.assertRaises(workflow.WorkflowError):
                workflow.validate_task(task)

    def test_json_schema_is_an_object(self):
        self.task["json_schema"] = "not a schema object"
        with self.assertRaisesRegex(workflow.WorkflowError, "JSON_SCHEMA"):
            workflow.validate_task(self.task)

    def test_worker_lock_prevents_overlap(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with workflow.worker_lock(port):
            with self.assertRaisesRegex(workflow.WorkflowError, "WORKER_BUSY"):
                with workflow.worker_lock(port):
                    self.fail("second worker acquired lock")


class SafetyTests(unittest.TestCase):
    def test_existing_system_proxy_is_used_only_for_child(self):
        original = {"TEST_SETTING": "keep"}
        result, source = workflow.cli_environment(original, proxy_reader=lambda: "127.0.0.1:7897")
        self.assertEqual(result["HTTPS_PROXY"], "http://127.0.0.1:7897")
        self.assertEqual(source, "windows_user_proxy")
        self.assertNotIn("HTTPS_PROXY", original)
        self.assertIn("127.0.0.1", result["NO_PROXY"])
        self.assertEqual(result["AGY_CLI_DISABLE_AUTO_UPDATE"], "true")

    def test_explicit_proxy_environment_is_preserved(self):
        with mock.patch.object(workflow, "windows_system_proxy") as reader:
            result, source = workflow.cli_environment({"HTTPS_PROXY": "http://existing:8080"}, proxy_reader=reader)
            self.assertEqual(result["HTTPS_PROXY"], "http://existing:8080")
            self.assertEqual(source, "inherited")
            reader.assert_not_called()

    def test_complex_proxy_is_not_guessed(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "EXPLICIT_HTTP_PROXY"):
            workflow.cli_environment({}, proxy_reader=lambda: "http=localhost:80;https=localhost:81")

    def test_unconfigured_proxy_uses_existing_direct_behavior(self):
        result, source = workflow.cli_environment({}, proxy_reader=lambda: None)
        self.assertEqual(source, "direct")
        self.assertNotIn("HTTPS_PROXY", result)

    def test_failed_or_missing_preflight_rejected(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "PREFLIGHT_REQUIRED"):
            workflow.require_receipt(None)
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            receipt = root / "failed"
            receipt.mkdir()
            for name in ("planned-commands.txt", "system-state.txt", "restore-point.txt", "ROLLBACK.md"):
                (receipt / name).write_text("not a successful receipt", encoding="utf-8")
            with self.assertRaises(workflow.WorkflowError):
                workflow.require_receipt(receipt, expected_root=root)

    def test_redirect_is_refused(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "REDIRECT_REFUSED"):
            workflow.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://example.test")

    def test_default_switch_endpoint_is_not_callable(self):
        manager = object.__new__(workflow.Manager)
        with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_NOT_ALLOWED"):
            manager.request("POST", "/api/accounts/switch")

    def test_secrets_and_emails_redacted(self):
        text = 'user@example.test Authorization: Bearer secret123 access_token="secret456" refresh_token="secret789"'
        result = workflow.redact(text)
        for secret in ("user@example.test", "secret123", "secret456", "secret789"):
            self.assertNotIn(secret, result)

    def test_quoted_secret_with_spaces_or_escapes_is_fully_redacted(self):
        for text in ('admin_password="synthetic first second"', "api_key='synthetic first second'",
                     'access_token="synthetic \\"first\\" second"'):
            with self.subTest(text=text):
                redacted = workflow.redact(text)
                for part in ("synthetic", "first", "second"):
                    self.assertNotIn(part, redacted)

    def test_nonsecret_token_status_fields_are_not_redacted(self):
        result = workflow.safe_object({"token_refreshed_via_manager": True, "credential_payload_sha256": "hash"})
        self.assertTrue(result["token_refreshed_via_manager"])
        self.assertEqual(result["credential_payload_sha256"], "[redacted]")

    def test_tokens_never_returned_from_identity_match(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            (root / "accounts").mkdir()
            (root / "accounts.json").write_text(json.dumps({"accounts": [{"id": "synthetic"}], "current_account_id": "synthetic", "current_target_ide": "agy"}))
            (root / "accounts/synthetic.json").write_text(json.dumps({"token": {"refresh_token": "synthetic-secret"}}))
            manager = object.__new__(workflow.Manager)
            manager.directory = root
            result = workflow.identify(manager, reader=lambda: "synthetic-secret")
            self.assertEqual(result["account_ref"], workflow.account_ref("synthetic"))
            self.assertNotIn("synthetic-secret", json.dumps(result))
            self.assertTrue(result["manager_matches"])

    def test_cli_payload_normalizes_expiry_without_exposing_token(self):
        account_data = {"token": {"access_token": "access-secret", "refresh_token": "refresh-secret",
                                   "expiry_timestamp": workflow.time.time() + 3600}}
        payload = workflow.build_cli_payload(account_data)
        self.assertEqual(payload["auth_method"], "consumer")
        self.assertEqual(payload["token"]["token_type"], "Bearer")
        self.assertEqual(payload["token"]["access_token"], "access-secret")
        self.assertTrue(payload["token"]["expiry"].endswith("Z"))

    @unittest.skipUnless(workflow.os.name == "nt", "Windows Credential Manager shape")
    def test_credential_writer_uses_windows_api_shape(self):
        payload = {"token": {"access_token": "a", "refresh_token": "r", "expiry": "2099-01-01T00:00:00Z"},
                   "auth_method": "consumer"}
        with mock.patch.object(workflow.ctypes, "WinDLL") as load:
            library = mock.Mock()
            write = mock.Mock(return_value=1)
            library.CredWriteW = write
            load.return_value = library
            workflow.write_cli_credential_payload(payload)
            self.assertTrue(write.called)
            credential = write.call_args.args[0]._obj
            self.assertEqual(credential.Type, 1)
            self.assertEqual(getattr(credential.TargetName, "value", credential.TargetName), "gemini:antigravity")
            self.assertEqual(getattr(credential.UserName, "value", credential.UserName), "antigravity")

    def test_expired_or_missing_cli_token_is_rejected(self):
        for token in ({}, {"access_token": "a", "refresh_token": "r", "expiry_timestamp": 0},
                      {"access_token": "a", "refresh_token": "r", "expiry_timestamp": float("nan")},
                      {"access_token": "a", "refresh_token": "r", "expiry_timestamp": True}):
            with self.subTest(token=token):
                with self.assertRaises(workflow.WorkflowError):
                    workflow.build_cli_payload({"token": token})


class NativeSwitchRegressionTests(unittest.TestCase):
    def setUp(self):
        import sqlite3
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "accounts").mkdir()
        self.index = {"version":"2.0", "accounts":[{"id":"target","email":"target@example.test"},
                      {"id":"original","email":"original@example.test"}], "current_account_id":"original",
                      "unknown_setting":{"owner":"owner@example.test"}}
        (self.root / "accounts.json").write_bytes(json.dumps(self.index,separators=(",", ":")).encode())
        self.items={}
        for identifier in ("target","original"):
            item=account(identifier)
            item["email"]=identifier+"@example.test"
            item["token"]={"access_token":identifier+"-synthetic-access","refresh_token":identifier+"-synthetic-refresh",
                           "expiry_timestamp":workflow.time.time()+3600}
            item["device_profile"]={key:identifier+"-"+key for key in ("machine_id","mac_machine_id","dev_device_id","sqm_id")}
            self.items[identifier]=item
            (self.root/"accounts"/(identifier+".json")).write_text(json.dumps(item),encoding="utf-8")
        self.original_credential=workflow.build_cli_payload(self.items["original"])
        self.state={"value":self.original_credential}
        self.reader=lambda:self.state["value"]
        self.writer=mock.Mock(side_effect=lambda value:self.state.update(value=value))
        self.manager=mock.Mock()
        self.manager.directory=self.root
        self.manager.accounts.return_value=[self.items["target"],self.items["original"]]
        self.manager.switch_cli.side_effect=self.native
        (self.root/"oauth_creds.json").write_text(json.dumps({"refresh_token":"original-synthetic-refresh","preserve":"secret"}))
        (self.root/"google_accounts.json").write_text(json.dumps({"active":"original@example.test","old":["older@example.test"]}))
        self.storage=self.root/"storage.json"
        self.storage.write_text(json.dumps({"other_setting":42}),encoding="utf-8")
        self.db=self.root/"state.vscdb"
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY,value TEXT)")
            connection.executemany("INSERT INTO ItemTable VALUES (?,?)",[("storage.serviceMachineId","before-device"),("unrelated","preserved")])
        self.storage_paths=[]
        self.original_bytes={p:p.read_bytes() for p in [self.root/"accounts.json",self.root/"accounts/target.json",
            self.root/"accounts/original.json",self.root/"oauth_creds.json",self.root/"google_accounts.json",self.storage]}

    def native(self, identifier):
        import sqlite3
        item=workflow.read_json(self.root/"accounts"/(identifier+".json"))
        item["last_used"]=1234
        (self.root/"accounts"/(identifier+".json")).write_text(json.dumps(item))
        self.state["value"]=workflow.build_cli_payload(item)
        index=workflow.read_json(self.root/"accounts.json")
        index.update(current_account_id=identifier,current_target_ide="agy")
        (self.root/"accounts.json").write_text(json.dumps(index))
        (self.root/"oauth_creds.json").write_text(json.dumps({"refresh_token":item["token"]["refresh_token"],"access_token":item["token"]["access_token"],"expiry_date":item["token"]["expiry_timestamp"]*1000,"token_type":"Bearer"}))
        (self.root/"google_accounts.json").write_text(json.dumps({"active":item["email"],"old":[]}))
        for path in self.storage_paths:
            data=workflow.read_json(path);profile=item["device_profile"]
            fields={"machineId":"machine_id","macMachineId":"mac_machine_id","devDeviceId":"dev_device_id","sqmId":"sqm_id"}
            data["telemetry"]={key:profile[field] for key,field in fields.items()}
            data.update({"telemetry."+key:profile[field] for key,field in fields.items()})
            data["storage.serviceMachineId"]=profile["dev_device_id"]
            path.write_text(json.dumps(data))
            with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
                connection.execute("INSERT OR REPLACE INTO ItemTable VALUES ('storage.serviceMachineId',?)",(profile["dev_device_id"],))
        return {"http_status":200}

    def switch(self, **kwargs):
        return workflow.update_cli_target(self.manager,workflow.account_ref("target"),reader=self.reader,writer=self.writer,
            credential_directory=self.root,storage_paths=self.storage_paths,**kwargs)

    def assert_restored(self):
        self.assertEqual(self.state["value"],self.original_credential)
        for path,raw in self.original_bytes.items():
            self.assertEqual(path.read_bytes(),raw,str(path))

    def test_native_switch_preserves_metadata_and_never_manually_writes_credentials(self):
        result=self.switch()
        self.assertEqual(result["switch_method"],"manager_native_api")
        self.assertEqual(result["fingerprint_storage_status"],"no_legacy_storage_present")
        self.manager.switch_cli.assert_called_once_with("target")
        self.writer.assert_not_called()
        saved=workflow.read_json(self.root/"accounts.json")
        self.assertEqual(saved,{**self.index,"current_account_id":"target","current_target_ide":"agy"})
        workflow.save_json(self.root/"audit.json",saved)
        self.assertNotIn("owner@example.test",(self.root/"audit.json").read_text())

    def test_native_switch_can_use_a_proxy_routing_disabled_account(self):
        self.items['target']['proxy_disabled'] = True
        (self.root/'accounts/target.json').write_text(json.dumps(self.items['target']))
        self.assertEqual(self.switch()['status'], 'SWITCHED')

    def test_existing_native_cli_file_is_verified_and_rolled_back_if_stale(self):
        native_file = self.root/'antigravity-cli/antigravity-oauth-token'
        native_file.parent.mkdir()
        native_file.write_text(json.dumps(self.original_credential))
        original = native_file.read_bytes()
        with self.assertRaisesRegex(workflow.WorkflowError, 'SWITCH_NATIVE_CLI_FILE_READBACK_FAILED'):
            self.switch()
        self.assertEqual(native_file.read_bytes(), original)
        self.assert_restored()

    def test_native_cli_file_written_by_the_backend_is_verified(self):
        native_file = self.root/'antigravity-cli/antigravity-oauth-token'
        native_file.parent.mkdir()
        native_file.write_text(json.dumps(self.original_credential))
        native = self.native
        def with_native_file(identifier):
            response = native(identifier)
            native_file.write_text(json.dumps(self.reader()))
            return response
        self.manager.switch_cli.side_effect = with_native_file
        result = self.switch()
        self.assertEqual(result['native_cli_file_status'], 'verified')

    def test_complete_native_fingerprint_and_database_are_verified(self):
        self.storage_paths=[self.storage]
        result=self.switch()
        self.assertEqual(result["fingerprint_storage_status"],"verified_existing_storage")
        self.assertTrue(workflow.fingerprint_matches(workflow.read_json(self.storage),self.items["target"]["device_profile"]))
        self.assertEqual(workflow.fingerprint_database_value(self.db),("target-dev_device_id",))

    def test_bad_fallback_credentials_trigger_exact_native_and_file_rollback(self):
        def native(identifier):
            result=self.native(identifier)
            if identifier=="target":
                (self.root/"oauth_creds.json").write_text(json.dumps({"refresh_token":"wrong"}))
            return result
        self.manager.switch_cli.side_effect=native
        with self.assertRaisesRegex(workflow.WorkflowError,"FILE_CREDENTIAL_READBACK_FAILED"):
            self.switch()
        self.assert_restored()
        self.assertEqual(self.manager.switch_cli.call_args_list,[mock.call("target"),mock.call("original")])

    def test_bad_fingerprint_restores_storage_and_only_the_database_row(self):
        import sqlite3
        self.storage_paths=[self.storage]
        def native(identifier):
            result=self.native(identifier)
            if identifier=="target":
                self.storage.write_text(json.dumps({"wrong_fingerprint":True}))
                with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
                    connection.execute("UPDATE ItemTable SET value='new unrelated value' WHERE key='unrelated'")
            return result
        self.manager.switch_cli.side_effect=native
        with self.assertRaisesRegex(workflow.WorkflowError,"FINGERPRINT_READBACK_FAILED"):
            self.switch()
        self.assert_restored()
        self.assertEqual(workflow.fingerprint_database_value(self.db),("before-device",))
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(connection.execute("SELECT value FROM ItemTable WHERE key='unrelated'").fetchone(),("new unrelated value",))

    def test_unchanged_wrong_database_fingerprint_is_not_reported_as_success(self):
        import sqlite3
        self.storage_paths=[self.storage]
        def native(identifier):
            result=self.native(identifier)
            if identifier=="target":
                with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
                    connection.execute("UPDATE ItemTable SET value='before-device' WHERE key='storage.serviceMachineId'")
            return result
        self.manager.switch_cli.side_effect=native
        with self.assertRaisesRegex(workflow.WorkflowError,"DATABASE_FINGERPRINT_READBACK_FAILED"):
            self.switch()
        self.assert_restored()

    def test_index_metadata_change_is_preserved_without_guessing_native_ownership(self):
        def native(identifier):
            result=self.native(identifier)
            if identifier=="target":
                index=workflow.read_json(self.root/"accounts.json");index.pop("unknown_setting")
                (self.root/"accounts.json").write_text(json.dumps(index))
            return result
        self.manager.switch_cli.side_effect=native
        with self.assertRaisesRegex(workflow.WorkflowError,"ROLLBACK_INCOMPLETE.*index_metadata_change_preserved"):
            self.switch()
        self.manager.switch_cli.assert_called_once_with("target")
        self.writer.assert_not_called()
        self.assertNotIn("unknown_setting",workflow.read_json(self.root/"accounts.json"))

    def test_wrong_complete_credential_triggers_verified_rollback(self):
        def native(identifier):
            result=self.native(identifier)
            if identifier=="target":self.state["value"]["auth_method"]="wrong"
            return result
        self.manager.switch_cli.side_effect=native
        with self.assertRaisesRegex(workflow.WorkflowError,"CREDENTIAL_READBACK_FAILED"):
            self.switch()
        self.assert_restored()

    def test_partial_native_failure_restores_all_completed_effects(self):
        def native(identifier):
            result=self.native(identifier)
            if identifier=="target":raise workflow.WorkflowError("MANAGER_SWITCH_HTTP_400: synthetic failure")
            return result
        self.manager.switch_cli.side_effect=native
        with self.assertRaisesRegex(workflow.WorkflowError,"HTTP_400"):
            self.switch()
        self.assert_restored()

    def test_uncertain_submitted_switch_never_races_server_with_rollback(self):
        def native(identifier):
            self.native(identifier)
            raise workflow.WorkflowError("NATIVE_SWITCH_OUTCOME_UNKNOWN")
        self.manager.switch_cli.side_effect=native
        with self.assertRaisesRegex(workflow.WorkflowError,"OUTCOME_UNKNOWN"):
            self.switch()
        self.manager.switch_cli.assert_called_once_with("target")
        self.writer.assert_not_called()
        self.assertEqual(workflow.read_json(self.root/"accounts.json")["current_account_id"],"target")

    def test_native_conflict_never_restores_another_in_progress_switch(self):
        self.manager.switch_cli.side_effect=workflow.WorkflowError("MANAGER_SWITCH_HTTP_409")
        with self.assertRaisesRegex(workflow.WorkflowError,"HTTP_409"):
            self.switch()
        self.writer.assert_not_called()
        self.manager.switch_cli.assert_called_once_with("target")
        self.assert_restored()

    def test_concurrent_file_edit_is_preserved_after_verification_failure(self):
        actual_identify=workflow.identify
        calls=[0]
        def identify(*args,**kwargs):
            calls[0]+=1
            if calls[0]==2:
                (self.root/"google_accounts.json").write_text(json.dumps({"active":"concurrent@example.test"}))
                raise workflow.WorkflowError("synthetic verification failure")
            return actual_identify(*args,**kwargs)
        with mock.patch.object(workflow,"identify",side_effect=identify):
            with self.assertRaisesRegex(workflow.WorkflowError,"ROLLBACK_INCOMPLETE.*concurrent_file"):
                self.switch()
        self.assertEqual(workflow.read_json(self.root/"google_accounts.json")["active"],"concurrent@example.test")
        self.writer.assert_not_called()

    def test_refreshed_flags_and_identity_are_checked_before_native_switch(self):
        for change in ({"validation_blocked":True},{"quota":{"is_forbidden":True}},{"id":"other"}):
            with self.subTest(change=change):
                self.manager.request.side_effect=lambda *args:(self.root/"accounts/target.json").write_text(json.dumps({**self.items["target"],**change}))
                with self.assertRaises(workflow.WorkflowError):self.switch()
                self.manager.switch_cli.assert_not_called()
                self.writer.assert_not_called()

    def test_committed_switch_is_reported_when_outer_manager_cleanup_fails(self):
        @contextlib.contextmanager
        def failed_cleanup(*args,**kwargs):
            self.assertEqual(kwargs["mode"],"desktop")
            yield self.manager
            raise workflow.WorkflowError("synthetic manager cleanup failed")
        stdout=io.StringIO()
        actual_switch=workflow.update_cli_target
        with mock.patch.object(workflow,"require_receipt",return_value={"synthetic":True}), \
             mock.patch.object(workflow,"worker_lock",side_effect=contextlib.nullcontext), \
             mock.patch.object(workflow,"Manager",return_value=self.manager), \
             mock.patch.object(workflow,"manager_session",side_effect=failed_cleanup), \
             mock.patch.object(workflow,"update_cli_target",side_effect=lambda *a,**k:actual_switch(self.manager,workflow.account_ref("target"),reader=self.reader,writer=self.writer,credential_directory=self.root,storage_paths=[])) as operation, \
             contextlib.redirect_stdout(stdout):
            code=workflow.main(["switch","--account-ref",workflow.account_ref("target"),"--audit-dir",str(self.root)])
        result=json.loads(stdout.getvalue())
        self.assertEqual(code,27)
        self.assertEqual(result["status"],"SWITCHED_CLEANUP_FAILED")
        self.assertEqual(result["completed_operation"]["switch_method"],"manager_native_api")
        operation.assert_called_once()


class NativeSwitchAPITests(unittest.TestCase):
    def test_native_optional_id_token_is_account_bound_and_key_order_independent(self):
        item={"token":{"access_token":"synthetic-access","refresh_token":"synthetic-refresh",
              "expiry_timestamp":workflow.time.time()+3600,"id_token":"selected-synthetic-id-token"}}
        core=workflow.build_cli_payload(item)
        payload={"id_token":"selected-synthetic-id-token","auth_method":core["auth_method"],"token":core["token"]}
        self.assertTrue(workflow.native_credential_matches(payload,item))
        self.assertTrue(workflow.native_credential_matches(core,item))
        self.assertFalse(workflow.native_credential_matches({**payload,"id_token":"other-account-id-token"},item))
        self.assertFalse(workflow.native_credential_matches({**payload,"unknown_field":True},item))

    def test_native_api_uses_camel_case_explicit_agy_and_empty_200_response(self):
        manager=object.__new__(workflow.Manager)
        manager.base="http://127.0.0.1:8046";manager.secret="synthetic-private-secret";manager.opener=mock.MagicMock()
        response=mock.MagicMock();response.status=200;response.read.return_value=b""
        manager.opener.open.return_value.__enter__.return_value=response
        result=manager.switch_cli("synthetic-target")
        request=manager.opener.open.call_args.args[0]
        self.assertEqual(request.full_url,"http://127.0.0.1:8046/api/accounts/switch")
        self.assertEqual(json.loads(request.data),{"accountId":"synthetic-target","targetIde":"agy"})
        self.assertEqual(result["http_status"],200)
        self.assertNotIn("synthetic-private-secret",json.dumps(result))

    def test_native_api_refuses_path_traversal_before_request(self):
        manager=object.__new__(workflow.Manager)
        for identifier in ("../other","bad/id",None):
            with self.assertRaisesRegex(workflow.WorkflowError,"INVALID_LOCAL_ACCOUNT_ID"):
                manager.switch_cli(identifier)

    def test_native_api_transport_failure_reports_uncertain_outcome(self):
        manager=object.__new__(workflow.Manager)
        manager.base="http://127.0.0.1:8046";manager.secret="synthetic";manager.opener=mock.Mock()
        manager.opener.open.side_effect=workflow.urllib.error.URLError("synthetic disconnect")
        with self.assertRaisesRegex(workflow.WorkflowError,"OUTCOME_UNKNOWN"):
            manager.switch_cli("synthetic-target")

    def test_disconnect_reset_and_partial_response_are_uncertain(self):
        manager=object.__new__(workflow.Manager)
        manager.base="http://127.0.0.1:8046";manager.secret="synthetic";manager.opener=mock.Mock()
        for error in (workflow.http.client.RemoteDisconnected(),ConnectionResetError(),workflow.http.client.IncompleteRead(b"",10)):
            with self.subTest(error=type(error).__name__):
                manager.opener.open.side_effect=error
                with self.assertRaisesRegex(workflow.WorkflowError,"OUTCOME_UNKNOWN"):
                    manager.switch_cli("synthetic-target")


class ManagerSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "gui_config.json"
        self.config.write_bytes(b'{"original": true}\n')
        self.manager = mock.Mock()
        self.manager.directory = self.root
        self.process = mock.Mock()
        self.process.poll.return_value = None

    def test_running_manager_is_not_started_or_stopped(self):
        with mock.patch.object(workflow, "require_receipt"), mock.patch.object(workflow.subprocess, "Popen") as start:
            with workflow.manager_session(self.manager, start=True, preflight_dir="synthetic"):
                pass
        start.assert_not_called()

    def test_owned_native_backend_uses_hidden_startup_and_cli_switch_capability(self):
        self.manager.accounts.side_effect=[workflow.WorkflowError("MANAGER_UNAVAILABLE"),[]]
        with mock.patch.object(workflow,"require_receipt"), mock.patch.object(workflow.subprocess,"Popen",return_value=self.process) as start:
            with workflow.manager_session(self.manager,start=True,executable=sys.executable,preflight_dir="synthetic",mode="desktop"):
                self.assertTrue(self.manager.desktop_started)
                self.assertFalse(self.manager.headless_started)
        self.assertEqual(start.call_args.args[0][1],"--minimized")
        self.assertEqual(start.call_args.kwargs["startupinfo"].wShowWindow,0)
        self.process.terminate.assert_called_once()

    def test_unknown_switch_preserves_owned_native_backend_and_its_config(self):
        self.manager.accounts.side_effect=[workflow.WorkflowError("MANAGER_UNAVAILABLE"),[]]
        self.process.pid=1234
        def start(*args,**kwargs):
            self.config.write_bytes(b'{"native_session":true}')
            return self.process
        with mock.patch.object(workflow,"require_receipt"), mock.patch.object(workflow.subprocess,"Popen",side_effect=start):
            with self.assertRaisesRegex(workflow.WorkflowError,"OUTCOME_UNKNOWN"):
                with workflow.manager_session(self.manager,start=True,executable=sys.executable,preflight_dir="synthetic",mode="desktop"):
                    self.manager.preserve_process_due_unknown_switch=True
                    raise workflow.WorkflowError("NATIVE_SWITCH_OUTCOME_UNKNOWN")
        self.process.terminate.assert_not_called()
        self.assertTrue(self.manager.manager_left_running)
        self.assertEqual(self.manager.manager_pid,1234)
        self.assertEqual(self.config.read_bytes(),b'{"native_session":true}')

    def test_missing_preflight_prevents_headless_process_start(self):
        with mock.patch.object(workflow.subprocess, "Popen") as start:
            with self.assertRaisesRegex(workflow.WorkflowError, "PREFLIGHT_REQUIRED"):
                with workflow.manager_session(self.manager, start=True):
                    pass
        start.assert_not_called()

    def test_owned_headless_process_uses_loopback_and_restores_config(self):
        self.manager.accounts.side_effect = [workflow.WorkflowError("MANAGER_UNAVAILABLE"), []]
        def start(*args, **kwargs):
            self.config.write_bytes(b'{"temporary": true}\n')
            self.assertEqual(args[0][1], "--headless")
            self.assertEqual(kwargs["env"]["ABV_BIND_LOCAL_ONLY"], "true")
            self.assertNotIn("ABV_API_KEY", kwargs["env"])
            return self.process
        with mock.patch.object(workflow, "require_receipt"), mock.patch.object(workflow.subprocess, "Popen", side_effect=start):
            with workflow.manager_session(self.manager, start=True, executable=sys.executable, preflight_dir="synthetic"):
                self.assertEqual(self.config.read_bytes(), b'{"temporary": true}\n')
        self.process.terminate.assert_called_once()
        self.assertEqual(self.config.read_bytes(), b'{"original": true}\n')

    def test_cached_inventory_is_read_only_and_excludes_tokens(self):
        (self.root / "accounts").mkdir()
        (self.root / "accounts.json").write_text(json.dumps({"accounts": [{"id": "target"}], "current_account_id": "target"}))
        item = account("target")
        item["token"] = {"refresh_token": "synthetic-secret"}
        (self.root / "accounts/target.json").write_text(json.dumps(item))
        manager = object.__new__(workflow.Manager)
        manager.directory = self.root
        with mock.patch.object(manager, "request", side_effect=workflow.WorkflowError("MANAGER_UNAVAILABLE")):
            result = manager.accounts(allow_local=True)
        self.assertEqual(manager.inventory_source, "local_cache")
        self.assertNotIn("synthetic-secret", json.dumps(result))
        self.assertTrue(result[0]["is_current"])

    def test_invalid_api_json_does_not_fall_back_to_cache(self):
        manager = object.__new__(workflow.Manager)
        manager.directory = self.root
        with mock.patch.object(manager, "request", side_effect=workflow.WorkflowError("MANAGER_INVALID_JSON")):
            with self.assertRaisesRegex(workflow.WorkflowError, "INVALID_JSON"):
                manager.accounts(allow_local=True)

    def test_cached_account_uses_documented_missing_field_defaults(self):
        (self.root / "accounts").mkdir()
        (self.root / "accounts.json").write_text(json.dumps({"accounts": [{"id": "target"}]}))
        item = account("target")
        for field in ("disabled", "proxy_disabled", "validation_blocked", "protected_models", "live_limited_models"):
            item.pop(field)
        (self.root / "accounts/target.json").write_text(json.dumps(item))
        manager = object.__new__(workflow.Manager)
        manager.directory = self.root
        with mock.patch.object(manager, "request", side_effect=workflow.WorkflowError("MANAGER_UNAVAILABLE")):
            result = manager.accounts(allow_local=True)
        self.assertEqual(workflow.rank_accounts(result, MODEL, now=NOW)["status"], "CANDIDATE")

    def test_startup_failure_preserves_unowned_config_changes(self):
        self.manager.accounts.side_effect = workflow.WorkflowError("MANAGER_UNAVAILABLE")
        def start(*args, **kwargs):
            self.config.write_bytes(b'{"concurrent": true}\n')
            self.process.poll.return_value = 1
            return self.process
        with mock.patch.object(workflow, "require_receipt"), mock.patch.object(workflow.subprocess, "Popen", side_effect=start):
            with self.assertRaisesRegex(workflow.WorkflowError, "CONFIG_CHANGED"):
                with workflow.manager_session(self.manager, start=True, executable=sys.executable, preflight_dir="synthetic"):
                    pass
        self.assertEqual(self.config.read_bytes(), b'{"concurrent": true}\n')


class WorkflowIntegrationTests(unittest.TestCase):
    def setUp(self):
        process_patch = mock.patch.object(workflow, "cli_processes", return_value=[])
        process_patch.start()
        self.addCleanup(process_patch.stop)
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        task = {"task_id": "integration", "model": MODEL, "prompt": "Synthetic reasoning only",
                "acceptance": "Expected synthetic answer", "read_only": True, "retry_safe": True, "max_attempts": 3}
        task_path = self.root / "task.json"
        task_path.write_text(json.dumps(task))
        self.args = SimpleNamespace(preflight_dir="synthetic-receipt", cli=sys.executable, workspace=str(self.root),
                                    task=str(task_path), audit_dir=str(self.root / "audit"), timeout=30)

    def execute(self, identity, completed):
        with mock.patch.object(workflow, "require_receipt", return_value={"synthetic": True}), \
                mock.patch.object(workflow, "worker_lock", side_effect=contextlib.nullcontext), \
                mock.patch.object(workflow, "Manager"), \
                mock.patch.object(workflow, "identify", side_effect=identity), \
                mock.patch.object(workflow.subprocess, "run", side_effect=completed) as runner:
            result = workflow.run_worker(self.args)
        return result, runner

    def identity(self, ref):
        return {"account_ref": ref, "manager_matches": True, "manager_target": "agy"}

    def completed(self, data, code):
        return subprocess.CompletedProcess([], code, stdout=data.encode(), stderr=b"")

    def test_persistent_quota_switch_then_success_preserves_both_attempts(self):
        old = self.identity("acct-old")
        exhausted = self.completed(stream(status="ERROR", response="", error="RESOURCE_EXHAUSTED: quota exhausted"), 1)
        first, first_runner = self.execute([old, old], [exhausted])
        self.assertEqual(first["status"], "QUOTA_EXHAUSTED")
        new = self.identity("acct-new")
        second, runner = self.execute([new, new], [self.completed(stream(), 0)])
        self.assertEqual(second["status"], "SUCCESS")
        state = workflow.read_json(self.root / "audit/task-integration.json")
        self.assertEqual([item["status"] for item in state["attempts"]], ["QUOTA_EXHAUSTED", "SUCCESS"])
        self.assertTrue(Path(first["result_file"]).is_file())
        self.assertTrue(Path(second["result_file"]).is_file())
        command = runner.call_args.args[0]
        self.assertEqual(command[command.index("--model") + 1], MODEL)
        self.assertIn("--mode", command)
        self.assertNotIn("--disable-slash-commands", command)
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertIs(runner.call_args.kwargs["shell"], False)
        self.assertEqual(command[command.index("--print") + 1], first_runner.call_args.args[0][command.index("--print") + 1])

    def test_account_changed_during_tool_free_reasoning_is_an_explicit_warning(self):
        result, _ = self.execute([self.identity("acct-old"), self.identity("acct-other")], [self.completed(stream(), 0)])
        self.assertEqual(result["status"], "SUCCESS")
        self.assertFalse(result["identity_stable"])
        self.assertEqual(result["warnings"], ["SHARED_IDENTITY_CHANGED_DURING_RUN"])
        self.assertEqual(result["identity_after"]["account_ref"], "acct-other")
        with self.assertRaisesRegex(workflow.WorkflowError, "NON_QUOTA_RETRY"):
            self.execute([self.identity("acct-other")], [])

    def test_worker_receives_the_authorized_absolute_workspace(self):
        identity = self.identity('acct-one')
        result, runner = self.execute([identity, identity], [self.completed(stream(), 0)])
        command = runner.call_args.args[0]
        prompt = command[command.index('--print') + 1]
        self.assertIn(Path(self.args.workspace).resolve().as_posix(), prompt)
        self.assertEqual(Path(runner.call_args.kwargs['cwd']), Path(self.args.workspace).resolve())
        self.assertEqual(command[command.index('--add-dir') + 1], str(Path(self.args.workspace).resolve()))

    def test_identity_change_after_tools_still_stops(self):
        tool = {"step_type": "tool", "tool_name": "write_to_file", "tool_info": {"name": "write_to_file"}}
        result, _ = self.execute([self.identity("acct-old"), self.identity("acct-other")],
                                 [self.completed(stream(tool=tool), 0)])
        self.assertEqual(result["status"], "IDENTITY_CHANGED_DURING_RUN")
        self.assertEqual(result["worker_status"], "SUCCESS")
        self.assertEqual(result["tool_steps"], 1)

    def test_identity_change_during_quota_failure_does_not_authorize_rotation(self):
        result, _ = self.execute([self.identity("acct-old"), self.identity("acct-other")],
            [self.completed(stream(status="ERROR", response="", error="RESOURCE_EXHAUSTED: quota exhausted"), 1)])
        self.assertEqual(result["status"], "IDENTITY_CHANGED_DURING_RUN")
        self.assertEqual(result["worker_status"], "QUOTA_EXHAUSTED")

    def test_other_sessions_are_recorded_without_blocking_a_valid_call(self):
        sessions = [{"pid": 123, "created_at": "2026-10-12T00:00:00Z"}]
        with mock.patch.object(workflow, "cli_processes", return_value=sessions):
            identity = self.identity("acct-one")
            result, _ = self.execute([identity, identity], [self.completed(stream(), 0)])
        saved = workflow.read_json(result["result_file"])
        self.assertEqual(saved["other_cli_sessions"], sessions)
        self.assertTrue(saved["identity_stable"])

    def test_process_discovery_failure_is_advisory(self):
        with mock.patch.object(workflow, "cli_processes", side_effect=workflow.WorkflowError("CLI_PROCESS_DISCOVERY_FAILED")):
            identity = self.identity("acct-one")
            result, _ = self.execute([identity, identity], [self.completed(stream(), 0)])
        self.assertEqual(result["status"], "SUCCESS")
        self.assertIsNone(workflow.read_json(result["result_file"])["other_cli_sessions"])

    def test_identity_change_for_write_capable_task_remains_a_failure(self):
        task = workflow.read_json(self.args.task)
        task["read_only"] = False
        Path(self.args.task).write_text(json.dumps(task))
        result, _ = self.execute([self.identity("acct-old"), self.identity("acct-other")], [self.completed(stream(), 0)])
        self.assertEqual(result["status"], "IDENTITY_CHANGED_DURING_RUN")

    def test_requested_schema_without_structured_result_fails(self):
        task = workflow.read_json(self.args.task)
        task["json_schema"] = {"type": "object"}
        Path(self.args.task).write_text(json.dumps(task))
        identity = self.identity("acct-one")
        result, runner = self.execute([identity, identity], [self.completed(stream(), 0)])
        self.assertEqual(result["status"], "PROTOCOL_ERROR")
        self.assertIn("--json-schema", runner.call_args.args[0])

    def test_timeout_saved_and_not_automatically_retried(self):
        identity = self.identity("acct-one")
        result, runner = self.execute([identity, identity], [subprocess.TimeoutExpired("synthetic", 30)])
        self.assertEqual(result["status"], "TIMEOUT")
        self.assertEqual(runner.call_count, 1)
        with self.assertRaisesRegex(workflow.WorkflowError, "NON_QUOTA_RETRY"):
            self.execute([identity], [])

    def test_receipt_failure_prevents_any_process_or_manager_call(self):
        with mock.patch.object(workflow, "require_receipt", side_effect=workflow.WorkflowError("PREFLIGHT_FAILED")), \
                mock.patch.object(workflow.subprocess, "run") as runner, mock.patch.object(workflow, "Manager") as manager:
            with self.assertRaisesRegex(workflow.WorkflowError, "PREFLIGHT_FAILED"):
                workflow.run_worker(self.args)
            runner.assert_not_called()
            manager.assert_not_called()


if __name__ == "__main__":
    unittest.main()
