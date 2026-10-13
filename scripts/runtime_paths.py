"""Resolve native Windows runtime paths without scanning disks or exposing account data."""
import json
import os
from pathlib import Path
import shutil
import subprocess


def _absolute(value):
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise ValueError("Runtime path must be a nonempty path")
    return Path(os.path.expandvars(os.fspath(value))).expanduser().resolve()


def _selected(explicit, environment_name):
    if explicit is not None:
        return _absolute(explicit)
    value = os.environ.get(environment_name)
    return _absolute(value) if value else None


def cli_path(explicit=None):
    """Explicit/env paths never fall back; an absent default is returned for diagnostics."""
    selected = _selected(explicit, "AGY_SUBAGENT_CLI")
    if selected is not None:
        return selected
    local = os.environ.get("LOCALAPPDATA")
    default = _absolute(local) / "agy/bin/agy.exe" if local else Path.home() / "AppData/Local/agy/bin/agy.exe"
    default = default.resolve()
    if default.is_file():
        return default
    found = shutil.which("agy.exe")
    return _absolute(found) if found else default


def manager_directory(explicit=None):
    return _selected(explicit, "AGY_SUBAGENT_MANAGER_DIR") or (Path.home() / ".antigravity_tools").resolve()


def settings_path(explicit=None):
    return _selected(explicit, "AGY_SUBAGENT_SETTINGS") or (Path.home() / ".gemini/antigravity-cli/settings.json").resolve()


def _running_manager_paths():
    """Read only executable paths through CIM; never request prompts or CommandLine."""
    if os.name != "nt":
        return []
    script = r"""$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $items=@(Get-CimInstance Win32_Process -Filter "Name = 'antigravity-tools.exe'" | ForEach-Object { $_.ExecutablePath } | Where-Object { $_ }); ConvertTo-Json -InputObject $items -Compress"""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script], capture_output=True,
            check=True, timeout=15, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        # Process discovery is advisory; explicit configuration and PATH still work.
        return []
    try:
        records = json.loads(completed.stdout.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as error:
        raise ValueError("Manager executable discovery returned invalid JSON") from error
    if not isinstance(records, list) or any(not isinstance(row, str) or not row.strip() for row in records):
        raise ValueError("Manager executable discovery returned invalid executable paths")
    return [_absolute(row) for row in records]


def _existing_executable(path, source):
    if not path.is_file():
        raise FileNotFoundError(
            "Antigravity Tools executable from " + source + " was not found; "
            "set AGY_SUBAGENT_MANAGER_CLI or pass --manager-cli")
    return path


def manager_executable(explicit=None):
    """Use explicit/env, unique running executable, PATH, then limited install locations."""
    selected = _selected(explicit, "AGY_SUBAGENT_MANAGER_CLI")
    if selected is not None:
        return _existing_executable(selected, "explicit configuration")
    running = {os.path.normcase(str(path)): path for path in _running_manager_paths()}
    if len(running) > 1:
        raise ValueError("Multiple Antigravity Tools installations are running; set AGY_SUBAGENT_MANAGER_CLI or pass --manager-cli")
    if running:
        return _existing_executable(next(iter(running.values())), "the running process")
    found = shutil.which("antigravity-tools.exe")
    if found:
        return _existing_executable(_absolute(found), "PATH")
    roots = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(_absolute(local) / "Programs")
    for name in ("ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(name):
            roots.append(_absolute(os.environ[name]))
    for root in roots:
        for directory in ("Antigravity Tools", "antigravity-tools"):
            candidate = (root / directory / "antigravity-tools.exe").resolve()
            if candidate.is_file():
                return candidate
    raise FileNotFoundError(
        "Antigravity Tools executable was not found in running processes, PATH or common install directories; "
        "set AGY_SUBAGENT_MANAGER_CLI or pass --manager-cli")
