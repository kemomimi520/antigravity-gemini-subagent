# Environment and discovery

For model-assisted installation and missing dependencies, follow [installation.md](installation.md) ([简体中文](installation.zh-CN.md)).

## Supported environment

The full workflow targets Windows. It uses Windows Credential Manager and PowerShell process discovery, with a bundled account core adapted from Antigravity Tools. The desktop manager remains an optional backend. Do not claim Linux/macOS support from upstream manager support or passing pure-Python fixtures.

Runtime and offline tests use the Python standard library; Python 3.11 has been tested. Choose a working interpreter by required features, not an exact patch-version gate. Native CLI must already be installed and signed in. Runtime helpers do not install software or perform login; follow the installation guide when setting up dependencies.

Required state:

- A logged-in native CLI account eligible for the chosen model.
- An upstream-compatible local account inventory. Existing Tools data can be reused; otherwise register the logged-in CLI identity with `register-current` under a valid preflight receipt.
- The same Windows user's credential context for helper and CLI (and optional Tools).
- The managed preflight provider below for CLI execution, refresh, switching and temporary settings changes.
- Outbound connectivity for native CLI and authorized Google account operations. Optional Tools administration stays on loopback.

## Paths

Command flags take precedence over environment variables and discovered/default locations:

| Setting | Override | Environment variable | Discovery/default |
| --- | --- | --- | --- |
| Native CLI | `models/run/doctor --cli` | `AGY_SUBAGENT_CLI` | Installed CLI/PATH and conventional local installation |
| Optional Tools executable | `--manager-cli` | `AGY_SUBAGENT_MANAGER_CLI` | Running process, PATH and conventional installation locations; Tools backend only |
| Account data | `--account-dir` / `--manager-dir` | `AGY_SUBAGENT_MANAGER_DIR` | User profile `.antigravity_tools`, retained for data compatibility |
| File-permission settings | `run/doctor --settings-file` | `AGY_SUBAGENT_SETTINGS` | User profile `.gemini/antigravity-cli/settings.json` |

Account-directory and backend overrides are accepted by all workflow entrypoints. `--account-backend builtin` is the default; it never discovers or starts the manager executable. `--account-backend tools` uses the optional desktop backend. Use explicit paths when discovery is absent or ambiguous. Binary hashes are audit metadata, not permission to execute an unexpected binary.

The settings override selects the file the helper temporarily edits. It does **not** change where `agy` loads configuration. Point it at the actual native settings file; otherwise the grant may have no effect. On Tools, the data directory must describe that backend. On builtin, GUI configuration and local management authentication are not required. See [native-accounts.md](native-accounts.md) for scope and unsupported proxy-pool account bindings.

Run `workflow.py doctor --audit-dir <audit-dir>` after configuration changes. Doctor inspects local prerequisites without starting CLI/manager, refreshing quota, switching identity or modifying settings. Its audit output is a workspace write. A healthy report does not substitute for live acceptance or preflight.

## Managed preflight

The package requires successful restore-point/file-backup preflight before live operations. `prepare.ps1` prioritizes existing `<user-profile>/.codex/managed-preflight.ps1`; only when absent does it use bundled `scripts/managed_preflight.ps1`. It requires administrator rights; when not elevated it uses installed `gsudo`. Follow the receiving user's authorization and host rules. Historical authorization does not transfer with this repository. The fallback retains the restore-point gate; it is not an option to bypass a failed host preflight.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File <skill>/scripts/prepare.ps1
# Include custom targets in the preflight backup:
powershell -NoProfile -ExecutionPolicy Bypass -File <skill>/scripts/prepare.ps1 -ManagerDir <manager-data-dir> -SettingsFile <native-settings-file>
```

Do not bypass the restore-point gate or invent receipts. The helper expects a receipt below `<user-profile>/.codex/rollback/`, action `gemini-cli-skill` or `gemini-cli-subagent`, age at most 24 hours, and nonempty `planned-commands.txt`, `system-state.txt`, `restore-point.txt` and `ROLLBACK.md`. Restore evidence must record a created or recent restore point. This is a local audit check, not cryptographically authenticated authorization.

`models`, `run`, `switch`, `register-current` and `accounts --refresh` require receipts. Local `doctor`, cached `accounts`, `identify`, offline tests and workspace documentation do not. Backups may contain full credentials: keep them local, out of source control. Ensure custom paths/effective fingerprint stores are covered by the selected preflight provider.

## Connectivity and shared state

The CLI preserves explicit proxy environment variables. Otherwise the helper can pass a simple enabled Windows user HTTP proxy to the child only. It does not change system proxy settings. Complex per-protocol strings require explicit configuration instead of guessed conversion. CLI child auto-update is disabled; there is no version allowlist.

Builtin account transport uses fixed Google HTTPS origins, applicable HTTP/HTTPS proxy settings and no redirects. It never calls a local manager endpoint or inference proxy. Optional Tools control uses `127.0.0.1`, the configured management port and in-memory authentication, with inherited proxies/redirects disabled. No manager chat/completions endpoint is used.

One native Windows credential is shared. The helper's operation lock does not isolate interactive sessions, manager GUI or Antigravity 2.0. Coordinate the entire switch/resume sequence. Do not promise independent per-worker accounts or an OS sandbox.

Primary sources: [CLI authentication](https://antigravity.google/docs/cli/getting-started/), [CLI headless behavior](https://antigravity.google/docs/cli/headless/), [native manager integration](https://github.com/lbjlaq/Antigravity-Manager/blob/main/src-tauri/src/modules/integration.rs), [gsudo](https://github.com/gerardog/gsudo). Check capabilities and postconditions after updates, not version numbers.
