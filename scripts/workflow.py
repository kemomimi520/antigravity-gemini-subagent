"""Antigravity CLI worker with bundled native accounts and optional Tools backend."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import difflib
import hashlib
import hmac
import http.client
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.error
import urllib.request
import urllib.parse
import uuid


class WorkflowError(Exception):
    pass


def sibling_module(name):
    spec = importlib.util.spec_from_file_location("agy_worker_" + name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def strict_json(data):
    """Reject ambiguous/non-JSON numbers before they enter persistent state."""
    def invalid(value):
        raise ValueError("non-finite JSON number")
    def finite(value):
        result = float(value)
        return result if math.isfinite(result) else invalid(value)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result
    try:
        return json.loads(data, parse_constant=invalid, parse_float=finite, object_pairs_hook=unique)
    except (ValueError, TypeError, RecursionError) as error:
        raise WorkflowError("INVALID_JSON: " + str(error)) from None


def read_json(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        return strict_json(stream.read())


def digest(value):
    return hashlib.sha256(value).hexdigest()


def account_ref(account_id):
    return "acct-" + digest(str(account_id).encode())[:12]


def redact(text):
    text = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "[email]", str(text), flags=re.I)
    text = re.sub(r"(?i)(Bearer\s+)\S+", r"\1[redacted]", text)
    # Redact a complete quoted secret first, including spaces and escaped quotes.
    text = re.sub(r'''(?i)(["']?(?:access_token|refresh_token|id_token|api_key|admin_password)["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''', r'\1"[redacted]"', text)
    text = re.sub(r'(?i)(["\']?(?:access_token|refresh_token|id_token|api_key|admin_password)["\']?\s*[:=]\s*)["\']?[^\s,"\'}]+["\']?', r'\1"[redacted]"', text)
    text = re.sub(r"\b(?:ya29\.[\w.-]+|1//[\w.-]+|sk-[\w-]{12,})", "[redacted]", text)
    return text


def safe_object(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [safe_object(item) for item in value]
    if isinstance(value, dict):
        return {key: ("[redacted]" if re.search(r"(?:access|refresh|id)_token|password|api_key|credential_payload", key, re.I)
                      and key not in {"input_tokens", "output_tokens", "thinking_tokens", "total_tokens", "cache_read_tokens"}
                      else safe_object(item)) for key, item in value.items()}
    return value


def atomic_write(path, data, *, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("xb") as stream:
        if path.exists():
            sibling_module("file_security").preserve_replacement_security(path, temporary)
        elif private:
            sibling_module("file_security").protect_new_private_file(temporary)
        stream.write(data)
    os.replace(temporary, path)


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")


def save_json(path, value):
    """Save a redacted audit record; never use this for application state."""
    atomic_write(path, json_bytes(safe_object(value)))


def audit(directory, event, value):
    path = Path(directory) / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + event + "-" + uuid.uuid4().hex[:8] + ".json")
    save_json(path, {"observed_at": datetime.now(timezone.utc).isoformat(), **value})
    return str(path.resolve())


def number(value, low, high):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and low <= value <= high and (not isinstance(value, float) or math.isfinite(value)))


def windows_system_proxy():
    if os.name != "nt":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            if winreg.QueryValueEx(key, "ProxyEnable")[0] != 1:
                return None
            return winreg.QueryValueEx(key, "ProxyServer")[0]
    except FileNotFoundError:
        return None


def cli_environment(environ=None, proxy_reader=windows_system_proxy):
    environment = dict(os.environ if environ is None else environ)
    # Preserve explicit proxy settings before adopting the Windows user proxy.
    source = "inherited" if any(environment.get(key) for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")) else "direct"
    if source == "direct":
        proxy = proxy_reader()
        if proxy:
            if not isinstance(proxy, str) or ";" in proxy or "=" in proxy:
                raise WorkflowError("SYSTEM_PROXY_REQUIRES_EXPLICIT_HTTP_PROXY_ENV")
            address = proxy if "://" in proxy else "http://" + proxy
            parsed = urllib.parse.urlsplit(address)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise WorkflowError("UNSUPPORTED_SYSTEM_PROXY")
            environment["HTTP_PROXY"] = address
            environment["HTTPS_PROXY"] = address
            source = "windows_user_proxy"
    bypass = environment.get("NO_PROXY", environment.get("no_proxy", ""))
    environment["NO_PROXY"] = ",".join(filter(None, [bypass, "localhost", "127.0.0.1", "::1"]))
    environment["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    return environment, source


def require_receipt(directory, expected_root=None, now=None):
    if not directory:
        raise WorkflowError("PREFLIGHT_REQUIRED: successful managed preflight receipt required")
    path = Path(directory).resolve()
    root = (Path(expected_root) if expected_root else Path.home() / ".codex" / "rollback").resolve()
    if path == root or root not in path.parents:
        raise WorkflowError("PREFLIGHT_INVALID: receipt must be under the managed rollback root")
    for name in ("planned-commands.txt", "system-state.txt", "restore-point.txt", "ROLLBACK.md"):
        if not (path / name).is_file() or not (path / name).stat().st_size:
            raise WorkflowError("PREFLIGHT_INCOMPLETE: " + name)
    state = (path / "system-state.txt").read_text(encoding="utf-8-sig")
    restore = (path / "restore-point.txt").read_text(encoding="utf-8-sig")
    if not re.search(r"^ActionName: gemini-cli-(skill|subagent)\s*$", state, re.M):
        raise WorkflowError("PREFLIGHT_WRONG_SCOPE")
    if not re.search(r"^(RestorePointCreatedNow|HasRecentRestorePointWithin7Days): True\s*$", restore, re.M):
        raise WorkflowError("PREFLIGHT_FAILED: no verified restore point")
    match = re.search(r"^Timestamp: (.+)$", state, re.M)
    try:
        stamp = datetime.fromisoformat(match[1].strip()).timestamp()
    except (ValueError, TypeError):
        raise WorkflowError("PREFLIGHT_INVALID_TIMESTAMP") from None
    age = (time.time() if now is None else now) - stamp
    if not 0 <= age <= 86400:
        raise WorkflowError("PREFLIGHT_STALE")
    return {"path": str(path), "restore_sha256": digest((path / "restore-point.txt").read_bytes())}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WorkflowError("MANAGER_REDIRECT_REFUSED")


class Manager:
    def __init__(self, directory=None):
        self.directory = sibling_module("runtime_paths").manager_directory(directory)
        config = read_json(self.directory / "gui_config.json")
        if not isinstance(config, dict) or not isinstance(config.get("proxy", {}), dict):
            raise WorkflowError("MANAGER_INVALID_CONFIG")
        proxy = config.get("proxy", {})
        port = proxy.get("port")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise WorkflowError("MANAGER_INVALID_PORT")
        self.base = "http://127.0.0.1:" + str(port)
        self.secret = proxy.get("admin_password") or proxy.get("api_key")
        if not isinstance(self.secret, str) or not self.secret:
            raise WorkflowError("MANAGER_AUTH_NOT_CONFIGURED")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, method, route):
        allowed = {("GET", "/api/accounts"), ("POST", "/api/accounts/refresh")}
        if (method, route) not in allowed:
            quota_match = re.fullmatch(r"/api/accounts/([A-Za-z0-9_-]+)/quota", route)
            if method != "GET" or quota_match is None:
                raise WorkflowError("MANAGER_ROUTE_NOT_ALLOWED")
            account_id = quota_match.group(1)
            if not account_id:
                raise WorkflowError("MANAGER_INVALID_ACCOUNT_ID")
        request = urllib.request.Request(self.base + route, method=method,
                                         data=b"{}" if method == "POST" else None,
                                         headers={"Authorization": "Bearer " + self.secret,
                                                  "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=180 if method == "POST" else 45) as response:
                return strict_json(response.read().decode("utf-8-sig"))
        except urllib.error.HTTPError as error:
            raise WorkflowError("MANAGER_HTTP_" + str(error.code)) from None
        except (urllib.error.URLError, TimeoutError):
            raise WorkflowError("MANAGER_UNAVAILABLE") from None
        except UnicodeError:
            raise WorkflowError("MANAGER_INVALID_JSON") from None
        except WorkflowError as error:
            if str(error).startswith("INVALID_JSON:"):
                raise WorkflowError("MANAGER_INVALID_JSON") from None
            raise

    def accounts(self, *, allow_local=False):
        try:
            result = self.request("GET", "/api/accounts")
            self.inventory_source = "http"
        except WorkflowError as error:
            if not allow_local or str(error) != "MANAGER_UNAVAILABLE":
                raise
            index = read_json(self.directory / "accounts.json")
            if not isinstance(index, dict) or not isinstance(index.get("accounts"), list):
                raise WorkflowError("LOCAL_ACCOUNT_INDEX_INVALID")
            cached = []
            fields = ("id", "email", "disabled", "proxy_disabled", "validation_blocked", "validation_blocked_until",
                      "protected_models", "live_limited_models", "quota")
            # Match the 4.8.4 Rust Account serde defaults; explicit invalid/null values stay invalid.
            defaults = {"disabled": False, "proxy_disabled": False, "validation_blocked": False,
                        "protected_models": [], "live_limited_models": {}}
            for item in index["accounts"]:
                identifier = item.get("id") if isinstance(item, dict) else None
                if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
                    raise WorkflowError("INVALID_LOCAL_ACCOUNT_ID")
                local = read_json(self.directory / "accounts" / (identifier + ".json"))
                if local.get("id") != identifier:
                    raise WorkflowError("LOCAL_ACCOUNT_ID_MISMATCH")
                cached.append({**{key: local.get(key, defaults.get(key)) for key in fields},
                               "is_current": identifier == index.get("current_account_id")})
            result = {"accounts": cached}
            self.inventory_source = "local_cache"
        accounts = result.get("accounts") if isinstance(result, dict) else None
        if not isinstance(accounts, list) or not accounts:
            raise WorkflowError("MANAGER_EMPTY_OR_INVALID_INVENTORY")
        ids = [item.get("id") for item in accounts if isinstance(item, dict)]
        if len(ids) != len(accounts) or any(not isinstance(item, str) or not item for item in ids) or len(set(ids)) != len(ids):
            raise WorkflowError("MANAGER_AMBIGUOUS_INVENTORY")
        return accounts

    def switch_cli(self, account_id):
        """Call the installed manager's native one-click switch with an explicit CLI target."""
        if not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", account_id):
            raise WorkflowError("INVALID_LOCAL_ACCOUNT_ID")
        request = urllib.request.Request(self.base + "/api/accounts/switch", method="POST",
            data=json_bytes({"accountId": account_id, "targetIde": "agy"}),
            headers={"Authorization": "Bearer " + self.secret, "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=180) as response:
                raw = response.read(8192)
                if response.status != 200 or raw.strip():
                    raise WorkflowError("NATIVE_SWITCH_UNEXPECTED_RESPONSE")
                return {"http_status": response.status, "switch_method": "manager_native_api"}
        except urllib.error.HTTPError as error:
            diagnostic = redact(error.read(8192).decode("utf-8", errors="replace"))
            raise WorkflowError("MANAGER_SWITCH_HTTP_" + str(error.code) + ": " + diagnostic) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException):
            # The server may still be executing a submitted switch. Never race it with rollback.
            raise WorkflowError("NATIVE_SWITCH_OUTCOME_UNKNOWN: inspect identity before further action") from None


def account_service(args):
    """Use the bundled native core unless Tools is explicitly selected."""
    backend = getattr(args, "account_backend", "builtin")
    if backend == "builtin":
        return sibling_module("native_accounts").NativeAccounts(getattr(args, "manager_dir", None),
                sys.modules.get(__name__) or SimpleNamespace(**globals()))
    if backend == "tools":
        return Manager(getattr(args, "manager_dir", None))
    raise WorkflowError("INVALID_ACCOUNT_BACKEND")


@contextlib.contextmanager
def manager_session(manager, *, start=False, executable=None, preflight_dir=None, timeout=30, mode="headless"):
    """Reuse a running manager; explicitly start headless refresh or native desktop switching."""
    if mode not in {"headless", "desktop"}:
        raise WorkflowError("INVALID_MANAGER_MODE")
    if getattr(manager, "is_builtin", False) is True:
        yield manager
        return
    if not start:
        yield manager
        return
    require_receipt(preflight_dir)
    try:
        manager.accounts()
    except WorkflowError as error:
        if str(error) != "MANAGER_UNAVAILABLE":
            raise
    else:
        yield manager
        return
    executable = sibling_module("runtime_paths").manager_executable(executable).resolve(strict=True)
    config_path = manager.directory / "gui_config.json"
    original_config = config_path.read_bytes()
    environment = dict(os.environ)
    for name in ("ABV_API_KEY", "API_KEY", "ABV_WEB_PASSWORD", "WEB_PASSWORD", "ABV_AUTH_MODE", "AUTH_MODE"):
        environment.pop(name, None)
    environment["ABV_BIND_LOCAL_ONLY"] = "true"
    startupinfo = None
    if os.name == "nt" and mode == "desktop":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
    arguments = [str(executable), "--headless"] if mode == "headless" else [str(executable), "--minimized"]
    process = subprocess.Popen(arguments, cwd=executable.parent,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env=environment, shell=False,
                               startupinfo=startupinfo,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    started_config = None
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise WorkflowError("MANAGER_" + mode.upper() + "_START_FAILED")
            try:
                manager.accounts()
                started_config = config_path.read_bytes()
                manager.headless_started = mode == "headless"
                manager.desktop_started = mode == "desktop"
                break
            except WorkflowError as error:
                if str(error) != "MANAGER_UNAVAILABLE":
                    raise
                time.sleep(0.2)
        else:
            raise WorkflowError("MANAGER_" + mode.upper() + "_START_TIMEOUT")
        yield manager
    finally:
        preserve_process = getattr(manager, "preserve_process_due_unknown_switch", False) is True
        if preserve_process:
            manager.manager_left_running = True
            manager.manager_pid = process.pid
        elif process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        current_config = config_path.read_bytes()
        if not preserve_process and current_config != original_config:
            if started_config is not None and current_config == started_config:
                atomic_write(config_path, original_config)
            else:
                raise WorkflowError("MANAGER_CONFIG_CHANGED_DURING_SESSION: preserved concurrent edits")


def rank_accounts(accounts, model, *, now=None, max_age=300, refreshed_after=None,
                  exclude=(), current_ref=None):
    now = time.time() if now is None else now
    rows = []
    for account in accounts:
        ref = account_ref(account["id"])
        reasons = []
        quota = account.get("quota") or {}
        if not isinstance(quota, dict):
            quota = {}
        models = quota.get("models", [])
        matches = [item for item in models if isinstance(item, dict) and item.get("name") == model] if isinstance(models, list) else []
        quota_model = model
        effort_match = re.fullmatch(r"(gemini-[a-zA-Z0-9._-]+)-(low|medium|high)", model)
        if not matches and effort_match and isinstance(models, list):
            tiered = effort_match[1] + "-tiered"
            matches = [item for item in models if isinstance(item, dict) and item.get("name") == tiered]
            if matches:
                quota_model = tiered
        guarded_models = {model, quota_model}
        # proxy_disabled controls the manager's API routing, not native CLI access.
        for flag in ("disabled", "validation_blocked"):
            if account.get(flag) is not False:
                reasons.append(flag + "_or_unknown")
        if quota.get("is_forbidden") is not False:
            reasons.append("forbidden_or_unknown")
        until = account.get("validation_blocked_until")
        if until is not None and (not number(until, 0, 1e12) or until > now):
            reasons.append("validation_cooldown")
        protected = account.get("protected_models", [])
        if not isinstance(protected, list) or any(not isinstance(v, str) for v in protected):
            reasons.append("invalid_protected_models")
        elif any(value in protected for value in guarded_models):
            reasons.append("protected_models")
        limited = account.get("live_limited_models", {})
        if not isinstance(limited, dict):
            reasons.append("invalid_live_limited_models")
        else:
            for guarded in guarded_models:
                if guarded in limited:
                    limit = limited[guarded]
                    if not isinstance(limit, dict) or not number(limit.get("until"), 0, 1e12):
                        reasons.append("invalid_live_limit")
                    elif limit["until"] > now or limit["until"] == 0:
                        reasons.append("live_limited_models")
        updated = quota.get("last_updated")
        if not number(updated, 0, 1e12) or not -30 <= now - updated <= max_age:
            reasons.append("stale_or_unknown_quota")
        if refreshed_after is not None and (not number(updated, 0, 1e12) or updated < refreshed_after):
            reasons.append("not_refreshed_this_round")
        values = []
        if len(matches) != 1:
            reasons.append("missing_or_ambiguous_model")
        elif not number(matches[0].get("percentage"), 0, 100):
            reasons.append("invalid_model_quota")
        else:
            values.append(matches[0]["percentage"])
        buckets = []
        groups = quota.get("quota_groups")
        if groups is not None and not isinstance(groups, list):
            reasons.append("invalid_quota_groups")
        elif groups:
            for group in groups:
                if not isinstance(group, dict):
                    reasons.append("invalid_quota_group")
                    continue
                group_buckets = group.get("buckets")
                is_gemini = "gemini" in str(group.get("display_name", "")).lower()
                if not isinstance(group_buckets, list):
                    reasons.append("invalid_quota_buckets")
                    continue
                for bucket in group_buckets:
                    if not isinstance(bucket, dict):
                        reasons.append("invalid_quota_bucket")
                        continue
                    if is_gemini or str(bucket.get("bucket_id", "")).startswith("gemini-"):
                        fraction = bucket.get("remaining_fraction")
                        if not number(fraction, 0, 1):
                            reasons.append("invalid_gemini_bucket")
                        else:
                            values.append(fraction * 100)
                            buckets.append({"id": bucket.get("bucket_id"), "remaining_percent": fraction * 100,
                                            "reset_time": bucket.get("reset_time")})
            if not buckets:
                reasons.append("missing_gemini_buckets")
        score = min(values) if values and not any(reason.startswith(("invalid", "missing")) for reason in reasons) else None
        if score is not None and score <= 0:
            reasons.append("quota_exhausted")
        if ref in exclude:
            reasons.append("already_attempted")
        rows.append({"account_ref": ref, "eligible": not reasons, "score_percent": score,
                     "quota_model": quota_model,
                     "last_updated": updated if number(updated, 0, 1e12) else None,
                     "is_cli_current": ref == current_ref, "manager_current": account.get("is_current") is True,
                     "buckets": buckets, "reasons": reasons})
    rows.sort(key=lambda row: (not row["eligible"], -(row["score_percent"] if row["score_percent"] is not None else -1),
                               not row["is_cli_current"], row["account_ref"]))
    eligible = [row for row in rows if row["eligible"]]
    return {"status": "CANDIDATE" if eligible else "NO_ELIGIBLE_ACCOUNT", "model": model,
            "inventory_count": len(rows), "selected": eligible[0]["account_ref"] if eligible else None,
            "accounts": rows}


def read_cli_credential_payload():
    if os.name != "nt":
        raise WorkflowError("CLI_IDENTITY_UNSUPPORTED_PLATFORM")

    class Credential(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    library = ctypes.WinDLL("advapi32", use_last_error=True)
    read = library.CredReadW
    read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(Credential))]
    read.restype = wintypes.BOOL
    free = library.CredFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    pointer = ctypes.POINTER(Credential)()
    if not read("gemini:antigravity", 1, 0, ctypes.byref(pointer)):
        raise WorkflowError("CLI_CREDENTIAL_UNAVAILABLE")
    try:
        data = ctypes.string_at(pointer.contents.CredentialBlob, pointer.contents.CredentialBlobSize)
        payload = strict_json(data.decode("utf-8"))
        if not isinstance(payload, dict):
            raise WorkflowError("CLI_CREDENTIAL_UNRECOGNIZED")
        token = payload.get("token")
        if not isinstance(token, dict) or not isinstance(token.get("refresh_token"), str) or not token["refresh_token"]:
            raise WorkflowError("CLI_CREDENTIAL_UNRECOGNIZED")
        return payload
    finally:
        free(pointer)


def write_cli_credential_payload(payload):
    if os.name != "nt":
        raise WorkflowError("CLI_CREDENTIAL_WRITE_UNSUPPORTED_PLATFORM")
    if not isinstance(payload, dict) or not isinstance(payload.get("token"), dict):
        raise WorkflowError("CLI_CREDENTIAL_PAYLOAD_INVALID")
    token = payload["token"]
    for key in ("access_token", "refresh_token", "expiry"):
        if not isinstance(token.get(key), str) or not token[key]:
            raise WorkflowError("CLI_CREDENTIAL_PAYLOAD_MISSING_" + key.upper())
    secret = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    class Credential(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    library = ctypes.WinDLL("advapi32", use_last_error=True)
    write = library.CredWriteW
    write.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
    write.restype = wintypes.BOOL
    target_wide = ctypes.c_wchar_p("gemini:antigravity")
    user_wide = ctypes.c_wchar_p("antigravity")
    buffer = (ctypes.c_ubyte * len(secret)).from_buffer_copy(secret)
    credential = Credential(0, 1, target_wide, None, wintypes.FILETIME(0, 0), len(secret), buffer, 2, 0, None, None, user_wide)
    if not write(ctypes.byref(credential), 0):
        raise WorkflowError("CLI_CREDENTIAL_WRITE_FAILED_" + str(ctypes.get_last_error()))


def read_cli_credential():
    payload = read_cli_credential_payload()
    return payload["token"]["refresh_token"]


def build_cli_payload(account):
    token = account.get("token") if isinstance(account, dict) else None
    if not isinstance(token, dict):
        raise WorkflowError("ACCOUNT_TOKEN_MISSING")
    for key in ("access_token", "refresh_token", "expiry_timestamp"):
        if key not in token or not isinstance(token[key], str if key != "expiry_timestamp" else (int, float)):
            raise WorkflowError("ACCOUNT_TOKEN_INVALID_" + key.upper())
        if key != "expiry_timestamp" and not token[key]:
            raise WorkflowError("ACCOUNT_TOKEN_INVALID_" + key.upper())
    if isinstance(token["expiry_timestamp"], bool) or not math.isfinite(float(token["expiry_timestamp"])):
        raise WorkflowError("ACCOUNT_TOKEN_INVALID_EXPIRY")
    expiry = float(token["expiry_timestamp"])
    if expiry > 10_000_000_000:
        # The native backend accepts both seconds and milliseconds.
        expiry = int(expiry) // 1000
    if expiry <= time.time():
        raise WorkflowError("ACCOUNT_TOKEN_EXPIRED_AFTER_REFRESH")
    expiry_text = datetime.fromtimestamp(expiry, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return {"token": {"access_token": token["access_token"], "token_type": "Bearer",
                       "refresh_token": token["refresh_token"], "expiry": expiry_text},
            "auth_method": "consumer"}


def native_credential_matches(observed, account):
    expected = build_cli_payload(account)
    # The installed native build also carries the selected account's ID token.
    # Retain it; older published integrations may omit this optional field.
    if "id_token" in observed:
        id_token = account.get("token", {}).get("id_token")
        if not isinstance(id_token, str) or not id_token:
            return False
        expected["id_token"] = id_token
    encode = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()
    return hmac.compare_digest(encode(observed), encode(expected))


def fingerprint_storage_paths(manager, *, process_records=None):
    """Resolve the native agy/classic storage priority, not any matching legacy profile."""
    config_path = manager.directory / "gui_config.json"
    config = read_json(config_path) if config_path.is_file() else ({} if getattr(manager, "is_builtin", False) is True else read_json(config_path))
    args = config.get("antigravity_args") or []
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise WorkflowError("FINGERPRINT_PATH_DISCOVERY_FAILED")
    configured_data = None
    for index, arg in enumerate(args):
        value = args[index + 1] if arg == "--user-data-dir" and index + 1 < len(args) else (
                arg.split("=", 1)[1] if arg.startswith("--user-data-dir=") else None)
        if value and Path(value).is_dir():
            configured_data = Path(value)
            break
    if configured_data:
        configured_storage = configured_data / "User/globalStorage/storage.json"
        if configured_storage.is_file():
            return [configured_storage.resolve()]
    if process_records is None:
        process_records = []
        if os.name == "nt":
            script = r"""[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); $items = @(Get-CimInstance Win32_Process -Filter "Name = 'Antigravity.exe'" | Where-Object { $_.ExecutablePath -notmatch 'antigravity[ _-]ide' -and $_.CommandLine -notmatch '--type=|--utility-sub-type' } | ForEach-Object { $taskDataDir = $null; if ($_.CommandLine -match '--user-data-dir(?:\s*=\s*|\s+)(?:"([^"]+)"|([^\s]+))') { if ($Matches[1]) { $taskDataDir = $Matches[1] } else { $taskDataDir = $Matches[2] } }; [pscustomobject]@{ executable = $_.ExecutablePath; user_data_dir = $taskDataDir } }); ConvertTo-Json -InputObject $items -Compress"""
            result = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True,
                                    timeout=15, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            process_records = json.loads(result.stdout.decode("utf-8-sig"))
    if not isinstance(process_records, list) or any(not isinstance(item, dict) for item in process_records):
        raise WorkflowError("FINGERPRINT_PATH_DISCOVERY_FAILED")
    distinct = {(item.get("executable"), item.get("user_data_dir")) for item in process_records}
    if len(distinct) > 1:
        # Upstream sysinfo ordering between different active classic installations is unspecified.
        raise WorkflowError("FINGERPRINT_LOCATION_AMBIGUOUS")
    active_executable, active_data = next(iter(distinct)) if distinct else (None, None)
    data = configured_data or (Path(active_data) if isinstance(active_data, str) and Path(active_data).is_dir() else None)
    if data:
        storage = data / "User/globalStorage/storage.json"
        if storage.is_file():
            return [storage.resolve()]
    executable = Path(active_executable) if isinstance(active_executable, str) else None
    if executable is None:
        configured_exe = config.get("antigravity_executable")
        if isinstance(configured_exe, str) and Path(configured_exe).is_file():
            executable = Path(configured_exe)
    if executable is None:
        for name in ("Antigravity",):
            for directory in (Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Programs",
                              Path(os.environ.get("ProgramFiles", "C:/Program Files")),
                              Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))):
                candidate = directory / name / (name + ".exe")
                if candidate.is_file():
                    executable = candidate
                    break
            if executable is not None:
                break
    if executable:
        portable = executable.parent / "data/user-data/User/globalStorage/storage.json"
        if portable.is_file():
            return [portable.resolve()]
    appdata = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
    # Native targetIde=agy uses classic storage only; an IDE profile is not a fallback.
    for name in ("Antigravity",):
        storage = appdata / name / "User/globalStorage/storage.json"
        if storage.is_file():
            return [storage.resolve()]
    return []


def fingerprint_database_value(path):
    import sqlite3
    try:
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            row = connection.execute("SELECT value FROM ItemTable WHERE key = 'storage.serviceMachineId'").fetchone()
    except sqlite3.Error:
        raise WorkflowError("FINGERPRINT_DB_READ_FAILED") from None
    return row


def fingerprint_matches(data, profile):
    fields = {"machineId": "machine_id", "macMachineId": "mac_machine_id",
              "devDeviceId": "dev_device_id", "sqmId": "sqm_id"}
    nested = data.get("telemetry", {})
    return (isinstance(nested, dict) and all(nested.get(key) == profile.get(field)
            and data.get("telemetry." + key) == profile.get(field) for key, field in fields.items())
            and data.get("storage.serviceMachineId") == profile.get("dev_device_id"))


def update_cli_target(manager, account_ref_value, *, refresh_token=True, reader=read_cli_credential_payload,
                      writer=write_cli_credential_payload, credential_directory=None, storage_paths=None):
    """Run and verify the selected native account backend, with owned recovery."""
    if not re.fullmatch(r"acct-[0-9a-f]{12}", account_ref_value or ""):
        raise WorkflowError("INVALID_ACCOUNT_REF")
    accounts = manager.accounts()
    matches = [item for item in accounts if account_ref(item.get("id")) == account_ref_value]
    if len(matches) != 1:
        raise WorkflowError("ACCOUNT_REF_UNKNOWN_OR_AMBIGUOUS")
    account_id = matches[0]["id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", account_id):
        raise WorkflowError("INVALID_LOCAL_ACCOUNT_ID")
    if matches[0].get("disabled") is not False:
        raise WorkflowError("ACCOUNT_DISABLED_OR_UNKNOWN")
    if matches[0].get("validation_blocked") is True or (matches[0].get("quota") or {}).get("is_forbidden") is True:
        raise WorkflowError("ACCOUNT_FORBIDDEN_OR_VALIDATION_BLOCKED")
    if refresh_token:
        manager.request("GET", "/api/accounts/" + account_id + "/quota")
    index_path = manager.directory / "accounts.json"
    original_index = index_path.read_bytes()
    index = read_json(index_path)
    if not isinstance(index, dict) or not isinstance(index.get("accounts"), list):
        raise WorkflowError("LOCAL_ACCOUNT_INDEX_INVALID")
    if not any(isinstance(item, dict) and item.get("id") == account_id for item in index["accounts"]):
        raise WorkflowError("LOCAL_ACCOUNT_INDEX_TARGET_MISSING")
    local_path = manager.directory / "accounts" / (account_id + ".json")
    account = read_json(local_path)
    if not isinstance(account, dict) or account.get("id") != account_id:
        raise WorkflowError("LOCAL_ACCOUNT_ID_MISMATCH")
    for flag in ("disabled", "validation_blocked"):
        if account.get(flag, False) is not False:
            raise WorkflowError("REFRESHED_ACCOUNT_DISABLED_OR_BLOCKED")
    until = account.get("validation_blocked_until")
    if until is not None and (not number(until, 0, 1e12) or until > time.time()):
        raise WorkflowError("REFRESHED_ACCOUNT_VALIDATION_COOLDOWN")
    if (account.get("quota") or {}).get("is_forbidden") is True:
        raise WorkflowError("REFRESHED_ACCOUNT_FORBIDDEN")
    build_cli_payload(account)  # Validate refreshed token shape before any switching effects.
    previous = reader()
    previous_identity = identify(manager, reader=lambda: previous["token"]["refresh_token"])
    previous_id = next(item["id"] for item in index["accounts"] if account_ref(item["id"]) == previous_identity["account_ref"])
    credential_directory = Path(credential_directory or Path.home() / ".gemini")
    native_cli_file = credential_directory / "antigravity-cli/antigravity-oauth-token"
    storage_paths = fingerprint_storage_paths(manager) if storage_paths is None else list(storage_paths)
    if len(storage_paths) > 1:
        raise WorkflowError("FINGERPRINT_LOCATION_AMBIGUOUS")
    paths = list(dict.fromkeys([index_path, local_path, manager.directory / "accounts" / (previous_id + ".json"),
            credential_directory / "oauth_creds.json", credential_directory / "google_accounts.json", native_cli_file, *storage_paths]))
    snapshots = {path: path.read_bytes() if path.is_file() else None for path in paths}
    databases = {path.with_name("state.vscdb"): fingerprint_database_value(path.with_name("state.vscdb"))
                 for path in storage_paths if path.with_name("state.vscdb").is_file()}
    owned = None
    owned_databases = None
    owned_credential = None
    try:
        if index_path.read_bytes() != original_index or any((path.read_bytes() if path.is_file() else None) != raw
                                                           for path, raw in snapshots.items()):
            raise WorkflowError("ACCOUNT_STATE_CHANGED_BEFORE_SWITCH")
        response = manager.switch_cli(account_id)
        owned_credential = reader()
        owned = {path: path.read_bytes() if path.is_file() else None for path in paths}
        owned_databases = {path: fingerprint_database_value(path) for path in databases}
        local = read_json(local_path)
        if local.get("id") != account_id:
            raise WorkflowError("LOCAL_ACCOUNT_ID_MISMATCH")
        payload = build_cli_payload(local)
        if not native_credential_matches(reader(), local):
            raise WorkflowError("SWITCH_CREDENTIAL_READBACK_FAILED")
        if native_cli_file.is_file() and not native_credential_matches(read_json(native_cli_file), local):
            raise WorkflowError("SWITCH_NATIVE_CLI_FILE_READBACK_FAILED")
        identity = identify(manager, reader=lambda: reader()["token"]["refresh_token"])
        if identity["account_ref"] != account_ref_value or not identity["manager_matches"] or identity["manager_target"] != "agy":
            raise WorkflowError("SWITCH_POSTCONDITION_FAILED")
        after_index = read_json(index_path)
        metadata = lambda data: {key: value for key, value in data.items() if key not in {"current_account_id", "current_target_ide"}}
        if metadata(after_index) != metadata(index):
            raise WorkflowError("NATIVE_SWITCH_ACCOUNT_METADATA_CHANGED")
        fallback = read_json(credential_directory / "oauth_creds.json")
        active = read_json(credential_directory / "google_accounts.json")
        expected_expiry = local["token"]["expiry_timestamp"]
        if expected_expiry <= 10_000_000_000:
            expected_expiry *= 1000
        if (fallback.get("refresh_token") != local["token"]["refresh_token"]
                or fallback.get("access_token") != local["token"]["access_token"]
                or fallback.get("expiry_date") != expected_expiry
                or fallback.get("token_type") != "Bearer"
                or fallback.get("id_token") != local["token"].get("id_token")
                or active.get("active") != local.get("email")):
            raise WorkflowError("SWITCH_FILE_CREDENTIAL_READBACK_FAILED")
        profile = local.get("device_profile")
        if not isinstance(profile, dict) or any(not isinstance(profile.get(key), str) or not profile[key]
                for key in ("machine_id", "mac_machine_id", "dev_device_id", "sqm_id")):
            raise WorkflowError("NATIVE_SWITCH_BOUND_FINGERPRINT_MISSING")
        if account.get("device_profile") is not None and profile != account["device_profile"]:
            raise WorkflowError("NATIVE_SWITCH_BOUND_FINGERPRINT_CHANGED")
        if storage_paths and not any(fingerprint_matches(read_json(path), profile) for path in storage_paths):
            raise WorkflowError("NATIVE_SWITCH_FINGERPRINT_READBACK_FAILED")
        for path, row in owned_databases.items():
            if row != (profile["dev_device_id"],):
                raise WorkflowError("NATIVE_SWITCH_DATABASE_FINGERPRINT_READBACK_FAILED")
    except Exception as error:
        if getattr(manager, "is_builtin", False) is True:
            failures = manager.restore_owned_switch()
            if failures:
                raise WorkflowError("SWITCH_ROLLBACK_INCOMPLETE: " + ",".join(failures)) from error
            raise
        if str(error).startswith(("NATIVE_SWITCH_OUTCOME_UNKNOWN", "MANAGER_SWITCH_HTTP_409")):
            manager.preserve_process_due_unknown_switch = True
            raise
        if str(error).startswith("ACCOUNT_STATE_CHANGED_BEFORE_SWITCH"):
            raise
        if str(error) == "NATIVE_SWITCH_ACCOUNT_METADATA_CHANGED":
            # Native serialization loss and an edit during the request cannot be distinguished.
            # Restoring a whole index (or calling native restore) could erase the external edit.
            raise WorkflowError("SWITCH_ROLLBACK_INCOMPLETE: index_metadata_change_preserved") from error
        owned = owned or {path: path.read_bytes() if path.is_file() else None for path in paths}
        owned_databases = owned_databases or {path: fingerprint_database_value(path) for path in databases}
        if any((path.read_bytes() if path.is_file() else None) != raw for path, raw in owned.items()):
            raise WorkflowError("SWITCH_ROLLBACK_INCOMPLETE: concurrent_file_edit_preserved") from error
        observed = reader()
        if (owned_credential is not None and not hmac.compare_digest(json_bytes(observed), json_bytes(owned_credential))) or observed["token"]["refresh_token"] not in (previous["token"]["refresh_token"], read_json(local_path)["token"]["refresh_token"]):
            raise WorkflowError("SWITCH_ROLLBACK_INCOMPLETE: concurrent_credential_edit_preserved") from error
        failures = []
        try:
            manager.switch_cli(previous_id)  # Reset the native manager's in-memory selection and fingerprint too.
        except WorkflowError as restore_error:
            if str(restore_error).startswith(("NATIVE_SWITCH_OUTCOME_UNKNOWN", "MANAGER_SWITCH_HTTP_409")):
                manager.preserve_process_due_unknown_switch = True
                raise WorkflowError("SWITCH_ROLLBACK_INCOMPLETE: native_restore_outcome_unknown") from error
            failures.append("native_restore")
        restored_owned = {path: path.read_bytes() if path.is_file() else None for path in paths}
        try:
            writer(previous)
            if not hmac.compare_digest(json_bytes(reader()), json_bytes(previous)):
                failures.append("credential_readback")
        except Exception:
            failures.append("credential_restore")
        for path, raw in snapshots.items():
            try:
                current = path.read_bytes() if path.is_file() else None
                if current != restored_owned[path]:
                    failures.append("concurrent_file_edit_preserved")
                elif raw is None and current is not None:
                    # Preserve newly created non-workspace files; never silently delete them for rollback.
                    failures.append("new_file_preserved")
                elif raw is not None and current != raw:
                    atomic_write(path, raw)
                    if path.read_bytes() != raw:
                        failures.append("file_readback")
            except Exception:
                failures.append("file_restore")
        import sqlite3
        for path, row in databases.items():
            try:
                with contextlib.closing(sqlite3.connect(path)) as connection, connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = connection.execute("SELECT value FROM ItemTable WHERE key = 'storage.serviceMachineId'").fetchone()
                    previous_profile = read_json(manager.directory / "accounts" / (previous_id + ".json")).get("device_profile") or {}
                    if current not in (row, owned_databases[path], (previous_profile.get("dev_device_id"),)):
                        failures.append("concurrent_database_edit_preserved")
                    elif row is None:
                        connection.execute("DELETE FROM ItemTable WHERE key = 'storage.serviceMachineId'")
                    else:
                        connection.execute("INSERT OR REPLACE INTO ItemTable (key,value) VALUES ('storage.serviceMachineId',?)", row)
            except Exception:
                failures.append("database_restore")
        if failures:
            raise WorkflowError("SWITCH_ROLLBACK_INCOMPLETE: " + ",".join(failures)) from error
        raise
    return {"status": "SWITCHED", "account_ref": account_ref_value, "target_ide": "agy",
            "switch_method": response.get("switch_method", "manager_native_api"), "credential_scope": "shared_cli_and_2.0",
            "account_backend": "builtin" if getattr(manager, "is_builtin", False) is True else "tools",
            "token_refreshed_via_manager": getattr(manager, "is_builtin", False) is not True,
            "quota_refreshed_via_manager": refresh_token and getattr(manager, "is_builtin", False) is not True,
            "fallback_credentials_verified": True, "bound_fingerprint_verified": True,
            "native_cli_file_status": "verified" if native_cli_file.is_file() else "not_present",
            "fingerprint_storage_status": "verified_existing_storage" if storage_paths else "no_legacy_storage_present",
            "identity": identity, "index_sha256": digest(index_path.read_bytes())}


def identify(manager, reader=read_cli_credential):
    refresh = reader()
    if not isinstance(refresh, str) or not refresh:
        raise WorkflowError("CLI_CREDENTIAL_UNRECOGNIZED")
    index = read_json(manager.directory / "accounts.json")
    if not isinstance(index, dict) or not isinstance(index.get("accounts"), list):
        raise WorkflowError("CLI_ACCOUNT_INDEX_INVALID")
    matches = []
    for summary in index.get("accounts", []):
        if not isinstance(summary, dict):
            raise WorkflowError("CLI_ACCOUNT_INDEX_INVALID")
        account_id = summary.get("id")
        if not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", account_id):
            raise WorkflowError("INVALID_LOCAL_ACCOUNT_ID")
        local = read_json(manager.directory / "accounts" / (account_id + ".json"))
        if not isinstance(local, dict) or not isinstance(local.get("token"), dict):
            raise WorkflowError("CLI_ACCOUNT_TOKEN_INVALID")
        candidate = local.get("token", {}).get("refresh_token")
        if isinstance(candidate, str) and hmac.compare_digest(candidate.encode(), refresh.encode()):
            matches.append(account_id)
    if len(matches) != 1:
        raise WorkflowError("CLI_IDENTITY_UNKNOWN_OR_AMBIGUOUS")
    return {"status": "IDENTIFIED", "account_ref": account_ref(matches[0]),
            "manager_matches": index.get("current_account_id") == matches[0],
            "manager_target": index.get("current_target_ide"),
            "credential_scope": "shared_cli_and_2.0"}


def classify(stdout, stderr, returncode, model):
    events = []
    try:
        for line in stdout.splitlines():
            if line.strip():
                event = strict_json(line)
                if not isinstance(event, dict):
                    raise ValueError()
                events.append(event)
    except (ValueError, TypeError, WorkflowError):
        return {"status": "PROTOCOL_ERROR", "reason": "invalid_stream_json", "exit_code": 22}
    results = [item.get("result") for item in events if item.get("event") == "result"]
    inits = [item.get("init") for item in events if item.get("event") == "init"]
    steps = [item.get("step_update") for item in events if item.get("event") == "step_update"]
    if (len(results) != 1 or not isinstance(results[0], dict)
            or events[-1].get("event") != "result" or any(not isinstance(step, dict) for step in steps)):
        return {"status": "PROTOCOL_ERROR", "reason": "missing_or_duplicate_terminal_result", "exit_code": 22}
    result = results[0]
    if inits and (len(inits) != 1 or not isinstance(inits[0], dict) or inits[0].get("model") != model):
        return {"status": "PROTOCOL_ERROR", "reason": "model_not_verified", "exit_code": 22}
    error = result.get("error", "")
    if not isinstance(error, str):
        error = json.dumps(error)
    diagnostics = error + "\n" + stderr
    tool_steps = [step for step in steps if step.get("step_type") == "tool" or step.get("tool_info") or step.get("subagent_info")]
    if any("tool_info" in step and not isinstance(step["tool_info"], dict) for step in tool_steps):
        return {"status": "PROTOCOL_ERROR", "reason": "invalid_tool_event", "exit_code": 22}
    conversation_ids = {item.get("conversation_id") for item in events
                        if item.get("event") == "init" and isinstance(item.get("conversation_id"), str)
                        and item.get("conversation_id")}
    if isinstance(result.get("conversation_id"), str) and result.get("conversation_id"):
        conversation_ids.add(result["conversation_id"])
    if len(conversation_ids) > 1 or any(not valid_conversation_id(value) for value in conversation_ids):
        return {"status": "PROTOCOL_ERROR", "reason": "inconsistent_conversation_id", "exit_code": 22}
    if any(step.get("conversation_id") and step["conversation_id"] not in conversation_ids for step in steps):
        return {"status": "PROTOCOL_ERROR", "reason": "inconsistent_step_conversation_id", "exit_code": 22}
    summary = {"cli_status": result.get("status"), "cli_exit_code": returncode,
               "model_requested": model, "model_init": inits[0].get("model") if inits else None,
               "runtime_model_verified": False,
               "conversation_id": next(iter(conversation_ids), None), "response": result.get("response"),
               "structured_output": result.get("structured_output"),
               "usage": result.get("usage"), "tool_steps": len(tool_steps),
               "tool_names": sorted({str(step.get("tool_name") or step.get("tool_info", {}).get("name", "unknown")) for step in tool_steps}),
               "tool_errors": [safe_object(step["tool_info"]["error"]) for step in tool_steps
                               if step.get("tool_info", {}).get("error")],
               "stderr": redact(stderr), "error": redact(error), "event_count": len(events)}
    terminal_success = returncode == 0 and result.get("status") == "SUCCESS"
    if not terminal_success:
        # Generic 429, capacity, auth and validation failures are not evidence of depleted quota.
        auth = re.search(r"\b(?:401|403)\b|invalid_grant|unauthenticated|authentication required|verify your account|eligibility check failed|not eligible for Antigravity", diagnostics, re.I)
        capacity = re.search(r"overload|capacity|(?:server|backend|service).{0,30}(?:busy|unavailable)|\b503\b", diagnostics, re.I)
        quota = re.search(r"quota.{0,40}(?:exhausted|exceeded|insufficient)|(?:exhausted|exceeded|insufficient).{0,40}quota", diagnostics, re.I)
        if quota and not auth and not capacity and result.get("status") == "ERROR":
            return {**summary, "status": "QUOTA_EXHAUSTED", "exit_code": 20}
        return {**summary, "status": "WORKER_ERROR", "exit_code": 21,
                "failure_kind": "AUTH_OR_ELIGIBILITY" if auth else "CAPACITY" if capacity else "NON_QUOTA_ERROR"}
    if len(inits) != 1 or not isinstance(inits[0], dict) or inits[0].get("model") != model:
        return {**summary, "status": "PROTOCOL_ERROR", "reason": "model_not_verified", "exit_code": 22}
    summary["model_verified"] = inits[0]["model"]
    if re.search(r"--mode.{0,60}has no effect|permission.{0,40}ignored", diagnostics, re.I):
        return {**summary, "status": "INCOMPLETE_POLICY", "exit_code": 21}
    permission = re.search(r"soft.denied|permission.{0,30}(?:denied|required)|requires? approval|not permitted|tool.{0,30}denied", diagnostics, re.I)
    failures = [(index, step) for index, step in enumerate(tool_steps) if step.get("tool_info", {}).get("error")]
    def tool_name(step):
        return step.get("tool_name") or step.get("tool_info", {}).get("name")
    def operation(step):
        parameters = step.get("tool_info", {}).get("parameters")
        if not isinstance(parameters, dict) or not parameters:
            return None
        # File tools may repair other arguments; only the same target is evidence of recovery.
        target_keys = ("TargetFile", "AbsolutePath", "FilePath", "file_path", "path", "target_file", "file")
        targets = [(key, parameters[key]) for key in target_keys if isinstance(parameters.get(key), str) and parameters[key]]
        if targets:
            return ("file", tuple(targets))
        if isinstance(parameters.get("CommandLine"), str):
            return ("command", parameters["CommandLine"], parameters.get("Cwd"))
        return ("parameters", json.dumps(parameters, sort_keys=True, ensure_ascii=True, allow_nan=False))
    recovered = bool(failures) and all(any(
        later.get("state") == "DONE" and tool_name(later) == tool_name(failed)
        and operation(failed) is not None and operation(later) == operation(failed)
        and not later.get("tool_info", {}).get("error") and "output" in later.get("tool_info", {})
        and type(later.get("step_index")) is int and type(failed.get("step_index")) is int
        and later.get("step_index", -1) > failed.get("step_index", -1)
        for later in tool_steps[index + 1:]) for index, failed in failures)
    if permission or (failures and not recovered):
        return {**summary, "status": "INCOMPLETE_TOOLS", "exit_code": 21}
    if recovered:
        summary["warnings"] = ["TOOL_ERROR_FOLLOWED_BY_SUCCESS_REVIEW_OUTPUT"]
        summary["tool_errors_recovered"] = True
    if not isinstance(result.get("response"), str) or not result["response"].strip():
        return {**summary, "status": "PROTOCOL_ERROR", "reason": "empty_response", "exit_code": 22}
    return {**summary, "status": "SUCCESS", "exit_code": 0}


@contextlib.contextmanager
def worker_lock(port=47963):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as lock:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            lock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            lock.bind(("127.0.0.1", port))
        except OSError:
            raise WorkflowError("WORKER_BUSY: shared credential permits one worker at a time") from None
        yield


def cli_processes():
    """Read only PID/start-time metadata; never expose other sessions' prompts."""
    if os.name != "nt":
        return []
    script = r"""$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $items=@(Get-CimInstance Win32_Process -Filter "Name = 'agy.exe'" | ForEach-Object { [pscustomobject]@{ pid=[int]$_.ProcessId; created_at=$_.CreationDate.ToUniversalTime().ToString('o') } }); ConvertTo-Json -InputObject $items -Compress"""
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                capture_output=True, check=True, timeout=15, shell=False,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        records = json.loads(result.stdout.decode("utf-8-sig"))
        if not isinstance(records, list) or any(not isinstance(row, dict)
                or type(row.get("pid")) is not int or row["pid"] <= 0
                or not isinstance(row.get("created_at"), str) for row in records):
            raise ValueError()
        return [{"pid": row["pid"], "created_at": row["created_at"]} for row in records]
    except (OSError, ValueError, subprocess.SubprocessError):
        raise WorkflowError("CLI_PROCESS_DISCOVERY_FAILED") from None


@contextlib.contextmanager
def workspace_file_permissions(workspace, read_only, enabled=False, settings_path=None):
    """Temporarily grant explicitly authorized workspace files; retain all deny/ask rules."""
    evidence = {"enabled": enabled, "status": "not_requested"}
    if not enabled:
        yield evidence
        return
    path = sibling_module("runtime_paths").settings_path(settings_path)
    if not path.exists():
        evidence["status"] = "settings_absent_using_cli_defaults"
        yield evidence
        return
    original = path.read_bytes()
    settings = strict_json(original.decode("utf-8-sig"))
    if not isinstance(settings, dict):
        raise WorkflowError("INVALID_CLI_SETTINGS")
    permissions = settings.setdefault("permissions", {})
    if not isinstance(permissions, dict) or not isinstance(permissions.setdefault("allow", []), list):
        raise WorkflowError("INVALID_CLI_PERMISSIONS")
    action = "read_file" if read_only else "write_file"
    rule = action + "(" + Path(workspace).resolve().as_posix() + ")"
    evidence.update(rule=rule, original_sha256=digest(original))
    if rule in permissions["allow"]:
        evidence["status"] = "already_allowed"
        yield evidence
        return
    permissions["allow"].append(rule)
    temporary = json_bytes(settings)
    if path.read_bytes() != original:
        raise WorkflowError("CLI_SETTINGS_CHANGED_BEFORE_GRANT")
    atomic_write(path, temporary)
    evidence.update(status="temporarily_allowed", temporary_sha256=digest(temporary))
    try:
        yield evidence
    finally:
        current = path.read_bytes() if path.exists() else None
        unchanged_semantics = False
        if current is not None:
            try:
                canonical = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False)
                unchanged_semantics = canonical(json.loads(current.decode("utf-8-sig"))) == canonical(settings)
            except (ValueError, UnicodeError):
                pass
        if current is not None and (current == temporary or unchanged_semantics) and path.read_bytes() == current:
            atomic_write(path, original)
            evidence["status"] = "restored"
        else:
            evidence["status"] = "concurrent_change_preserved"
            evidence["warning"] = "WORKSPACE_PERMISSION_CLEANUP_CONCURRENT_CHANGE"


def valid_conversation_id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value) is not None


def task_effort(task):
    suffix = task.get("model", "").rsplit("-", 1)[-1]
    return task.get("effort", suffix if suffix in {"low", "medium", "high"} else "high")


def validate_task(task):
    if not isinstance(task, dict):
        raise WorkflowError("INVALID_TASK")
    fields = {"task_id", "model", "effort", "prompt", "acceptance", "read_only", "retry_safe", "max_attempts",
              "json_schema", "conversation_id", "allow_workspace_files"}
    unknown = set(task) - fields
    if unknown:
        suggestions = []
        for key in sorted(unknown, key=str):
            similar = difflib.get_close_matches(str(key), sorted(fields), n=1)
            suggestions.append(str(key) + (" (did you mean " + similar[0] + "?)" if similar else ""))
        raise WorkflowError("UNKNOWN_TASK_FIELDS: " + ", ".join(suggestions))
    for key in ("task_id", "model", "prompt", "acceptance"):
        if not isinstance(task.get(key), str) or not task[key].strip():
            raise WorkflowError("TASK_FIELD_REQUIRED: " + key)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", task["task_id"]):
        raise WorkflowError("INVALID_TASK_ID")
    if not re.fullmatch(r"gemini-[a-zA-Z0-9._-]+", task["model"]):
        raise WorkflowError("EXPLICIT_GEMINI_MODEL_REQUIRED")
    if not isinstance(task_effort(task), str) or task_effort(task) not in {"low", "medium", "high"}:
        raise WorkflowError("INVALID_CLI_EFFORT")
    if "json_schema" in task and not isinstance(task["json_schema"], dict):
        raise WorkflowError("JSON_SCHEMA_MUST_BE_AN_OBJECT")
    if "json_schema" in task:
        schema = sibling_module("schema_validation")
        try:
            schema.validate_schema(task["json_schema"])
        except schema.SchemaError as error:
            raise WorkflowError("INVALID_JSON_SCHEMA: " + str(error)) from None
    if "conversation_id" in task and not valid_conversation_id(task["conversation_id"]):
        raise WorkflowError("INVALID_CONVERSATION_ID")
    for key in ("read_only", "retry_safe"):
        if not isinstance(task.get(key), bool):
            raise WorkflowError("EXPLICIT_BOOLEAN_REQUIRED: " + key)
    maximum = task.get("max_attempts", 3)
    if "allow_workspace_files" in task and not isinstance(task["allow_workspace_files"], bool):
        raise WorkflowError("EXPLICIT_BOOLEAN_REQUIRED: allow_workspace_files")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 1:
        raise WorkflowError("INVALID_ATTEMPT_LIMIT")


def attempt_digest(attempt):
    return digest(json.dumps(attempt, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode())


def validate_resume_review(task, state, task_hash, review):
    last = state["attempts"][-1]
    if not valid_conversation_id(last.get("conversation_id")):
        raise WorkflowError("PARTIAL_EFFECTS_REQUIRE_CONVERSATION")
    expected = {"task_id": task["task_id"], "task_sha256": task_hash,
                "last_attempt_sha256": attempt_digest(last), "conversation_id": last["conversation_id"]}
    if (not isinstance(review, dict) or review.get("safe_to_resume") is not True
            or any(review.get(key) != value for key, value in expected.items())
            or not isinstance(review.get("completed_effects"), list)
            or any(not isinstance(item, str) or not item.strip() for item in review["completed_effects"])
            or any(not isinstance(review.get(key), str) or not review[key].strip()
                   for key in ("verification", "remaining_work"))):
        raise WorkflowError("PARTIAL_EFFECTS_REVIEW_INVALID_OR_STALE")


def check_retry(task, state, task_hash, current, review=None):
    if (not isinstance(state, dict) or not isinstance(state.get("attempts", []), list)
            or any(not isinstance(attempt, dict) or not isinstance(attempt.get("status"), str)
                   for attempt in state.get("attempts", []))):
        raise WorkflowError("INVALID_TASK_STATE: preserve evidence and inspect the state file")
    attempts = state.get("attempts", [])
    if state.get("task_sha256", task_hash) != task_hash:
        raise WorkflowError("TASK_CHANGED: create a new task ID for changed input")
    if len(attempts) >= task.get("max_attempts", 3):
        raise WorkflowError("ATTEMPT_LIMIT_REACHED")
    if attempts:
        last = attempts[-1]
        if last.get("status") != "QUOTA_EXHAUSTED":
            raise WorkflowError("NON_QUOTA_RETRY_REFUSED")
        if not task["retry_safe"] or last.get("tool_steps") != 0:
            if review is None:
                raise WorkflowError("PARTIAL_EFFECTS_REQUIRE_REVIEW")
            validate_resume_review(task, state, task_hash, review)
        elif review is not None:
            validate_resume_review(task, state, task_hash, review)
        if any(item.get("account_ref") == current for item in attempts):
            raise WorkflowError("QUOTA_FAILED_ACCOUNT_ALREADY_ATTEMPTED")
    elif review is not None:
        raise WorkflowError("RESUME_REVIEW_WITHOUT_FAILED_ATTEMPT")


def partial_stream_evidence(stdout=None, stderr=None):
    """Keep interrupted conversation/tool evidence without inferring task completion."""
    raw = stdout if isinstance(stdout, bytes) else (stdout or "").encode("utf-8")
    errors = stderr if isinstance(stderr, bytes) else (stderr or "").encode("utf-8")
    conversations = set()
    tools = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        try:
            event = strict_json(line)
        except WorkflowError:
            continue
        if not isinstance(event, dict):
            continue
        identifier = event.get("conversation_id")
        if event.get("event") == "result" and isinstance(event.get("result"), dict):
            identifier = event["result"].get("conversation_id") or identifier
        if valid_conversation_id(identifier):
            conversations.add(identifier)
        step = event.get("step_update")
        if isinstance(step, dict):
            if valid_conversation_id(step.get("conversation_id")):
                conversations.add(step["conversation_id"])
            if step.get("step_type") == "tool" or step.get("tool_info") or step.get("subagent_info"):
                tools.append(step)
    return {"conversation_id": next(iter(conversations)) if len(conversations) == 1 else None,
            "tool_steps": len(tools), "tool_names": sorted({str(step.get("tool_name") or
                (step.get("tool_info") or {}).get("name", "unknown")) for step in tools
                if not step.get("tool_info") or isinstance(step["tool_info"], dict)}),
            "stdout_sha256": digest(raw), "stderr_sha256": digest(errors),
            "stderr": redact(errors.decode("utf-8", errors="replace"))}


def run_worker(args):
    receipt = require_receipt(args.preflight_dir)
    executable = sibling_module("runtime_paths").cli_path(args.cli).resolve(strict=True)
    workspace = Path(args.workspace).resolve(strict=True)
    if not workspace.is_dir() or executable.suffix.lower() != ".exe":
        raise WorkflowError("INVALID_CLI_OR_WORKSPACE")
    task_bytes = Path(args.task).read_bytes()
    task = strict_json(task_bytes.decode("utf-8-sig"))
    validate_task(task)
    task_hash = digest(task_bytes)
    state_path = Path(args.audit_dir) / ("task-" + task["task_id"] + ".json")
    manager = account_service(args)
    with worker_lock():
        identity = identify(manager)
        try:
            other_sessions = cli_processes()
        except WorkflowError:
            other_sessions = None  # Advisory discovery must not prevent an otherwise valid call.
        state = read_json(state_path) if state_path.exists() else {"task_sha256": task_hash, "attempts": []}
        review_path = getattr(args, "resume_review", None)
        review = read_json(review_path) if review_path else None
        check_retry(task, state, task_hash, identity["account_ref"], review)
        last = state["attempts"][-1] if state["attempts"] else None
        workspace_hash = digest(os.fsencode(str(workspace)))
        if last and (last.get("workspace_sha256") != workspace_hash if last.get("workspace_sha256")
                     else last.get("cwd") and Path(last["cwd"]).resolve() != workspace):
            raise WorkflowError("RETRY_WORKSPACE_CHANGED")
        conversation = (last.get("conversation_id") if last else None) or task.get("conversation_id")
        if conversation and not valid_conversation_id(conversation):
            raise WorkflowError("INVALID_CONVERSATION_ID")
        if last and task.get("conversation_id") and conversation != task["conversation_id"]:
            raise WorkflowError("RETRY_CONVERSATION_CHANGED")
        if state["attempts"] and (not identity["manager_matches"] or identity["manager_target"] != "agy"):
            raise WorkflowError("CLI_SWITCH_NOT_VERIFIED")
        attempt_dir = Path(args.audit_dir) / (task["task_id"] + "-attempt-" + str(len(state["attempts"]) + 1))
        attempt_dir.mkdir(parents=True, exist_ok=False)
        prompt = "You are an external Gemini worker. Follow this bounded task contract. Do not manage accounts, credentials, other agents or applications.\n"
        prompt += "Authorized workspace (absolute path): " + workspace.as_posix() + "\n"
        prompt += "Resolve task file names inside this workspace and use absolute paths in file tools. Do not guess another directory, use a scratch/home directory instead, or access unrelated files.\n"
        if task["read_only"]:
            prompt += "Read-only reasoning task. Do not modify files or run tools unless the task explicitly requires an authorized read.\n"
        if last and conversation:
            prompt += "Resume the interrupted task in this conversation. Preserve prior context and complete only remaining work; do not repeat completed effects.\n"
        if review:
            prompt += "Supervisor verified completed effects:\n" + json.dumps(review["completed_effects"], ensure_ascii=False) + "\n"
            prompt += "Supervisor verification:\n" + review["verification"] + "\nRemaining work:\n" + review["remaining_work"] + "\n"
        prompt += "Task:\n" + task["prompt"] + "\nAcceptance:\n" + task["acceptance"]
        command = [str(executable), "--print", prompt, "--model", task["model"], "--effort", task_effort(task),
                   "--output-format", "stream-json", "--print-timeout", str(args.timeout) + "s",
                   "--add-dir", str(workspace),
                   "--log-file", str((attempt_dir / "cli.log").resolve())]
        if task["read_only"]:
            command += ["--mode", "plan"]
        if "json_schema" in task:
            command += ["--json-schema", json.dumps(task["json_schema"], ensure_ascii=True)]
        if conversation:
            command += ["--conversation", conversation]
        started = time.time()
        environment, proxy_source = cli_environment()
        record = {"account_backend": "builtin" if getattr(manager, "is_builtin", False) is True else "tools",
                  "account_ref": identity["account_ref"], "status": "RUNNING", "started_at": started,
                  "model": task["model"], "effort": task_effort(task), "task_sha256": task_hash,
                  "cli_path": str(executable), "cli_sha256": digest(executable.read_bytes()), "cwd": str(workspace),
                  "workspace_sha256": workspace_hash,
                  "read_only": task["read_only"], "identity_before": identity,
                  "resumed_from": conversation,
                  "resume_source": "quota_attempt" if last and conversation else "task" if conversation else None,
                  "resume_review": review,
                  "other_cli_sessions": other_sessions,
                  "preflight": receipt, "proxy_source": proxy_source,
                  "argv": ["[prompt sha256=" + digest(prompt.encode()) + "]" if value == prompt else value for value in command]}
        state["attempts"].append(record)
        save_json(state_path, state)
        permission_evidence = {}
        try:
            with workspace_file_permissions(workspace, task["read_only"], task.get("allow_workspace_files", False),
                                            settings_path=getattr(args, "settings_file", None)) as permission_evidence:
                result = subprocess.run(command, cwd=workspace, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, timeout=args.timeout + 20, shell=False,
                                        env=environment,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            stdout = result.stdout.decode("utf-8", errors="replace")
            stderr = result.stderr.decode("utf-8", errors="replace")
            outcome = classify(stdout, stderr, result.returncode, task["model"])
            if conversation and outcome["status"] in {"SUCCESS", "QUOTA_EXHAUSTED"} and outcome.get("conversation_id") != conversation:
                outcome.update(status="PROTOCOL_ERROR", reason="requested_conversation_not_resumed", exit_code=22)
            if "json_schema" in task and outcome["status"] == "SUCCESS" and outcome.get("structured_output") is None:
                outcome.update(status="PROTOCOL_ERROR", reason="missing_structured_output", exit_code=22)
            elif "json_schema" in task and outcome["status"] == "SUCCESS":
                schema = sibling_module("schema_validation")
                try:
                    schema.validate_instance(outcome["structured_output"], task["json_schema"])
                except schema.SchemaError as error:
                    outcome.update(status="PROTOCOL_ERROR", reason="invalid_structured_output",
                                   schema_error=str(error), exit_code=22)
            outcome["stdout_sha256"] = digest(result.stdout)
            outcome["stderr_sha256"] = digest(result.stderr)
        except subprocess.TimeoutExpired as error:
            outcome = {**partial_stream_evidence(error.output, error.stderr), "status": "TIMEOUT", "exit_code": 23,
                       "reason": "no automatic retry; inspect partial effects"}
        except KeyboardInterrupt:
            outcome = {"status": "CANCELED", "exit_code": 21, "reason": "interrupted; inspect partial effects"}
        except (WorkflowError, OSError, ValueError) as error:
            outcome = {"status": "WORKER_ERROR", "exit_code": 21,
                       "failure_kind": "SUPERVISOR_OR_PROCESS_ERROR", "error": redact(str(error))}
        outcome["workspace_permissions"] = permission_evidence
        if permission_evidence.get("warning"):
            outcome.setdefault("warnings", []).append(permission_evidence["warning"])
        try:
            after = identify(manager)
            outcome["identity_after"] = after
            outcome["identity_stable"] = after["account_ref"] == identity["account_ref"]
            if after["account_ref"] != identity["account_ref"]:
                # Shared keyring changes do not invalidate a verified, tool-free reasoning response.
                # Preserve the uncertainty; never treat it as permission to retry or rotate accounts.
                if outcome.get("status") == "SUCCESS" and task["read_only"] and outcome.get("tool_steps") == 0:
                    outcome.setdefault("warnings", []).append("SHARED_IDENTITY_CHANGED_DURING_RUN")
                    outcome["account_attribution"] = "unverified_shared_credential_changed"
                else:
                    outcome = {**outcome, "worker_status": outcome.get("status"),
                               "status": "IDENTITY_CHANGED_DURING_RUN", "exit_code": 25}
        except (WorkflowError, OSError, ValueError) as error:
            outcome = {**outcome, "identity_stable": False, "worker_status": outcome.get("status"),
                       "status": "IDENTITY_UNVERIFIABLE_AFTER_RUN", "identity_error": redact(str(error)), "exit_code": 25}
        record.update(outcome)
        record["ended_at"] = time.time()
        for key in ("response", "structured_output"):
            if key in outcome and safe_object(outcome[key]) != outcome[key]:
                outcome[key + "_redacted"] = True
                record[key + "_redacted"] = True
        if "json_schema" in task and outcome.get("status") == "SUCCESS" and outcome.get("structured_output_redacted"):
            schema = sibling_module("schema_validation")
            try:
                schema.validate_instance(safe_object(outcome["structured_output"]), task["json_schema"])
            except schema.SchemaError:
                outcome.update(status="OUTPUT_REDACTED", exit_code=21,
                               reason="redaction_changed_structured_output_schema", worker_status="SUCCESS")
                record.update(outcome)
        # Finalize state first: a log cleanup failure must not leave a RUNNING attempt.
        save_json(attempt_dir / "result.json", record)
        save_json(state_path, state)
        cli_log = attempt_dir / "cli.log"
        if cli_log.exists():
            try:
                log_text = cli_log.read_text(encoding="utf-8", errors="replace")
                atomic_write(cli_log, redact(log_text).encode("utf-8"))
            except OSError:
                outcome.update(worker_status=outcome["status"], status="AUDIT_CLEANUP_FAILED", exit_code=29)
                outcome.setdefault("warnings", []).append("CLI_LOG_MAY_CONTAIN_UNREDACTED_DATA")
                record.update(outcome)
        save_json(attempt_dir / "result.json", record)
        save_json(state_path, state)
        return {**outcome, "account_backend": "builtin" if getattr(manager, "is_builtin", False) is True else "tools",
                "account_ref": identity["account_ref"], "result_file": str((attempt_dir / "result.json").resolve())}


def doctor(args):
    """Read dependencies and identity without launching CLI, refreshing or switching."""
    checks = {}
    paths = sibling_module("runtime_paths")
    cli = paths.cli_path(args.cli)
    checks["platform"] = {"ok": os.name == "nt", "supported": "Windows"}
    checks["cli"] = {"ok": cli.is_file() and cli.suffix.lower() == ".exe", "path": str(cli)}
    checks["settings"] = {"ok": True, "path": str(paths.settings_path(args.settings_file)),
                          "exists": paths.settings_path(args.settings_file).is_file()}
    if checks["settings"]["exists"]:
        try:
            settings = read_json(checks["settings"]["path"])
            if not isinstance(settings, dict) or not isinstance(settings.get("permissions", {}), dict):
                raise WorkflowError("INVALID_CLI_SETTINGS")
            for field in ("allow", "deny", "ask"):
                if field in settings.get("permissions", {}) and not isinstance(settings["permissions"][field], list):
                    raise WorkflowError("INVALID_CLI_PERMISSIONS")
        except (WorkflowError, OSError, ValueError) as error:
            checks["settings"].update(ok=False, reason=redact(str(error)))
    if getattr(args, "account_backend", "builtin") == "builtin":
        checks["account_backend"] = {"ok": True, "selected": "builtin", "tools_installation_required": False}
    else:
        try:
            executable = paths.manager_executable(args.manager_cli)
            checks["manager_executable"] = {"ok": True, "path": str(executable)}
        except (OSError, ValueError) as error:
            checks["manager_executable"] = {"ok": False, "reason": redact(str(error)), "required_for_startup": True}
    try:
        manager = account_service(args)
        accounts = manager.accounts(allow_local=True)
        checks["manager_inventory"] = {"ok": True, "count": len(accounts), "source": manager.inventory_source}
        checks["identity"] = {"ok": True, **identify(manager)}
    except (WorkflowError, OSError, ValueError) as error:
        checks["manager_or_identity"] = {"ok": False, "reason": redact(str(error))}
    try:
        receipt = require_receipt(args.preflight_dir)
        checks["preflight"] = {"ok": True, **receipt}
    except WorkflowError as error:
        checks["preflight"] = {"ok": False, "reason": str(error),
                               "external_script_present": (Path.home() / ".codex/managed-preflight.ps1").is_file()}
    try:
        with worker_lock():
            checks["worker_lock"] = {"ok": True}
    except WorkflowError as error:
        checks["worker_lock"] = {"ok": False, "reason": str(error)}
    required = [value for key, value in checks.items() if key != "manager_executable"]
    ready = all(value["ok"] for value in required)
    return {"status": "READY" if ready else "NOT_READY", "checks": checks, "exit_code": 0 if ready else 24,
            "inference_route": "native_agy", "runtime_model_verified": False,
            "mutations": "audit_record_only"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    paths = sibling_module("runtime_paths")
    for name in ("accounts", "identify", "models", "run", "switch", "doctor", "register-current"):
        command = commands.add_parser(name)
        command.add_argument("--audit-dir", required=True)
        command.add_argument("--manager-dir", "--account-dir", dest="manager_dir", help="upstream-compatible local account directory; also AGY_SUBAGENT_MANAGER_DIR")
        command.add_argument("--account-backend", choices=("builtin", "tools"), default="builtin", help="bundled native core (default), or existing Antigravity Tools")
        command.add_argument("--manager-cli", help="installed Antigravity Tools executable; also AGY_SUBAGENT_MANAGER_CLI")
        if name in {"accounts", "models", "run", "switch", "doctor", "register-current"}:
            command.add_argument("--preflight-dir")
        if name in {"accounts", "switch"}:
            command.add_argument("--start-manager", action="store_true", help="start installed manager if unavailable: headless for quota refresh, native desktop backend for switching")
        if name in {"models", "run", "doctor"}:
            command.add_argument("--cli", default=str(paths.cli_path()))
        if name in {"run", "doctor"}:
            command.add_argument("--settings-file", help="CLI settings for scoped file permissions; also AGY_SUBAGENT_SETTINGS")
        if name in {"models", "run"}:
            command.add_argument("--workspace", required=True)
        if name == "accounts":
            command.add_argument("--model", required=True)
            command.add_argument("--refresh", action="store_true")
            command.add_argument("--max-age", type=int, default=300, choices=range(1, 3601), metavar="1..3600")
            command.add_argument("--exclude", action="append", default=[])
            command.add_argument("--include-email", action="store_true", help="local account inspection only; never sent to Gemini")
        if name == "identify":
            command.add_argument("--expect")
            command.add_argument("--require-cli-target", action="store_true")
        if name == "run":
            command.add_argument("--task", required=True)
            command.add_argument("--resume-review", help="supervisor-verified checkpoint JSON for a quota-failed attempt with partial effects")
            command.add_argument("--timeout", type=int, default=300, choices=range(10, 1801), metavar="10..1800")
        if name == "switch":
            command.add_argument("--account-ref", required=True)
            command.add_argument("--no-refresh", action="store_true", help="use only when quota was refreshed separately")
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            result = doctor(args)
        elif args.command == "register-current":
            require_receipt(args.preflight_dir)
            if args.account_backend != "builtin":
                raise WorkflowError("REGISTER_CURRENT_REQUIRES_BUILTIN_BACKEND")
            with worker_lock():
                result = account_service(args).register_current()
        elif args.command == "run":
            result = run_worker(args)
        elif args.command == "switch":
            receipt = require_receipt(args.preflight_dir)
            try:
                other_sessions = cli_processes()
            except WorkflowError:
                other_sessions = None
            manager = account_service(args)
            with worker_lock(), manager_session(manager, start=args.start_manager, executable=args.manager_cli,
                                                 preflight_dir=args.preflight_dir, mode="desktop"):
                result = update_cli_target(manager, args.account_ref, refresh_token=not args.no_refresh)
            result["preflight"] = receipt
            result["other_cli_sessions"] = other_sessions
            if other_sessions:
                result["warnings"] = ["EXISTING_CLI_SESSIONS_CAN_WRITE_BACK_CREDENTIALS"]
            result["headless_started"] = getattr(manager, "headless_started", False)
            result["desktop_started"] = getattr(manager, "desktop_started", False)
        elif args.command == "models":
            receipt = require_receipt(args.preflight_dir)
            environment, proxy_source = cli_environment()
            with worker_lock():
                completed = subprocess.run([str(paths.cli_path(args.cli)), "models"], cwd=args.workspace, stdin=subprocess.DEVNULL,
                                           capture_output=True, timeout=45, shell=False, env=environment,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            output = completed.stdout.decode("utf-8", errors="replace")
            slugs = sorted(set(re.findall(r"(?m)^\s*(gemini-[a-zA-Z0-9._-]+)\s", output)))
            result = {"status": "SUCCESS" if completed.returncode == 0 and slugs else "MODEL_DISCOVERY_FAILED",
                      "models": slugs, "stdout": output, "stderr": completed.stderr.decode("utf-8", errors="replace"),
                      "preflight": receipt, "proxy_source": proxy_source,
                      "exit_code": 0 if completed.returncode == 0 and slugs else 21}
        else:
            manager = account_service(args)
            if args.command == "identify":
                result = identify(manager)
                if args.expect and args.expect != result["account_ref"]:
                    raise WorkflowError("UNEXPECTED_CLI_ACCOUNT")
                if args.require_cli_target and (not result["manager_matches"] or result["manager_target"] != "agy"):
                    raise WorkflowError("MANAGER_CLI_TARGET_NOT_VERIFIED")
            else:
                if args.start_manager and not args.refresh:
                    raise WorkflowError("HEADLESS_START_REQUIRES_REFRESH: cached inspection stays read-only")
                if args.refresh:
                    receipt = require_receipt(args.preflight_dir)
                # Cached inspection can read existing local account files without starting a process.
                session = manager_session(manager, start=args.start_manager, executable=args.manager_cli,
                                          preflight_dir=args.preflight_dir)
                with worker_lock(), session:
                    accounts = manager.accounts(allow_local=not args.refresh)
                    result = account_inventory(manager, args, accounts)
                if args.refresh:
                    result["preflight"] = receipt
                result["headless_started"] = getattr(manager, "headless_started", False)
        evidence = audit(args.audit_dir, args.command, result)
        result["evidence_file"] = evidence
        if args.command == "accounts" and args.include_email:
            print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        else:
            print(json.dumps(safe_object(result), ensure_ascii=True, allow_nan=False))
        return result.get("exit_code", 0)
    except (WorkflowError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        completed = locals().get("result")
        failure = {"status": "BLOCKED", "reason": redact(str(error)), "exit_code": 24}
        if args.command == "switch" and "outcome_unknown" in str(error).lower():
            failure.update(status="SWITCH_OUTCOME_UNKNOWN", exit_code=28)
        local_manager = locals().get("manager")
        if getattr(local_manager, "manager_left_running", False) is True:
            failure.update(manager_left_running=True, manager_pid=local_manager.manager_pid)
        if args.command == "switch" and isinstance(completed, dict) and completed.get("status") == "SWITCHED":
            failure.update(status="SWITCHED_CLEANUP_FAILED", exit_code=27)
            failure["completed_operation"] = safe_object(completed)
        failure["evidence_file"] = audit(args.audit_dir, "blocked", failure)
        print(json.dumps(failure, ensure_ascii=True))
        return failure["exit_code"]


def account_inventory(manager, args, accounts):
    previous_ids = {item["id"] for item in accounts}
    expected_refreshed = sum((item.get("quota") if isinstance(item.get("quota"), dict) else {}).get("is_forbidden") is not True
                             for item in accounts)
    refreshed_after = None
    stats = None
    if args.refresh:
        require_receipt(args.preflight_dir)
        refreshed_after = int(time.time())
        stats = manager.request("POST", "/api/accounts/refresh")
        accounts = manager.accounts()
        if previous_ids != {item["id"] for item in accounts}:
            raise WorkflowError("INVENTORY_CHANGED_DURING_REFRESH")
        if (not isinstance(stats, dict) or stats.get("total") != expected_refreshed
                or not all(isinstance(stats.get(key), int) and not isinstance(stats.get(key), bool)
                           and stats[key] >= 0 for key in ("total", "success", "failed"))
                or stats["success"] + stats["failed"] != stats["total"]):
            raise WorkflowError("REFRESH_COVERAGE_UNVERIFIED")
    try:
        current = identify(manager)["account_ref"]
    except WorkflowError:
        current = None
    result = rank_accounts(accounts, args.model, max_age=args.max_age, refreshed_after=refreshed_after,
                           exclude=args.exclude, current_ref=current)
    result["snapshot_kind"] = "refreshed" if args.refresh else ("cached_local_read_only" if getattr(manager, "inventory_source", "http") == "local_cache" else "cached_read_only")
    result["refresh_stats"] = {key: stats.get(key) for key in ("total", "success", "failed")} if stats else None
    if args.include_email:
        lookup = {account_ref(item["id"]): item.get("email") for item in accounts}
        # This opt-in mapping goes only to the local caller, never to saved audit or a model prompt.
        for row in result["accounts"]:
            row["local_ui_email"] = lookup[row["account_ref"]]
    result["exit_code"] = 0 if result["selected"] else 26
    return result


if __name__ == "__main__":
    sys.exit(main())
