# Installation guide

English | [简体中文](installation.zh-CN.md)

Follow this guide when installing through a model or manually. Install the skill where the receiving agent can discover it, and separately establish whether this computer can run real worker tasks.

## 1. Inspect the environment

Read [environment setup](environment.md). Check the platform, Python, native `agy` and existing account state. The default bundled core needs no Antigravity Tools installation. The complete runtime workflow supports Windows; other platforms can inspect the source and run applicable offline tests.

| Dependency | Purpose | If missing |
| --- | --- | --- |
| Python | Standard-library runtime helpers | Use a working Python distribution; 3.11 has been tested, with no patch-version gate |
| Antigravity CLI | Native inference and conversation continuation | Follow [Google installation instructions](https://antigravity.google/docs/getting-started?tab=cli); the user completes sign-in |
| Bundled account core | Identity checks, quota refresh and native switching | Included in this repository; reuse compatible data or enroll after native CLI sign-in |
| Antigravity Tools (optional) | Previous desktop backend and proxy-pool bindings not included in the port | Needed only for explicit `--account-backend tools`; follow [upstream installation instructions](https://github.com/lbjlaq/Antigravity-Manager#installation) |
| Valid local preflight record | Backup and restore-point checks before live operations | Use this skill's `scripts/prepare.ps1`, subject to the current host's authorization and preflight rules |

New users need no Tools GUI configuration or management key. The default `.antigravity_tools` directory name preserves data compatibility. Initialize an empty store with `register-current` after CLI sign-in; do not paste account tokens into a chat. See [bundled account operations](native-accounts.md) for scope and multi-account enrollment.

Keep existing applications and account configuration. Check capabilities instead of insisting on a particular version. Use the overrides in [environment.md](environment.md) for custom paths.

## 2. Install the skill files

Read the root [SKILL.md](../SKILL.md) and relevant scripts, obtain the source, and record the actual branch and commit. The repository root is the skill root. Use the installation name `antigravity-gemini-subagent`.

Honor a user-specified destination. Codex defaults to `$CODEX_HOME/skills`, or `~/.codex/skills` when `CODEX_HOME` is unset. Other agents need their own supported skills directory and actual loading verification.

Prefer a built-in skill installer when available. For this repository, the source path is `.` and the explicit destination name is `antigravity-gemini-subagent`. Otherwise place the complete downloaded or cloned source folder in the target directory, including `SKILL.md`, `agents`, `scripts` and `references`.

If a same-name directory exists, check its origin first. Reuse identical content. For an update, preserve local changes and a reversible copy before synchronizing reviewed files. Do not remove the entire skills directory or overwrite unrelated skills. Access private repositories through existing GitHub permissions; report inaccessible source without changing repository visibility.

## 3. Handle missing dependencies

If the user also requested missing software installation, follow the official or upstream sources above and retain host-required preflight, backups and permissions. Check the download source and procedure before execution. Do not upgrade or reinstall working applications as a side effect.

If asked only to install the skill, complete source installation and diagnostics, then report missing dependencies and their installation sources. Dependency installation does not authorize new account login, other people's credentials or broader account access. The user completes sign-in, OAuth authorization and any account import not already authorized. Keep account tokens, cookies and backups local, out of model inputs and GitHub.

The bundled core checks the native CLI identity against a matching account record. The user signs in through CLI, then `register-current` enrolls that identity under valid preflight. Only explicit Tools mode requires its configuration, management authentication and account data. Do not fabricate configuration or credentials to pass checks. Inference always stays in native CLI.

## 4. Verify usability

Run from the installed skill directory, using a separate audit directory inside the authorized workspace:

```powershell
python -B scripts/package_skill.py
python -B scripts/workflow.py doctor --audit-dir <audit-dir>
```

`doctor` inspects the local environment without launching CLI/manager, refreshing quota or switching accounts. It writes diagnostics. A `NOT_READY` result due solely to missing preflight is expected before preparation; missing CLI, identity or manager configuration needs resolution first.

For live acceptance, complete preflight, then check identity and discover models:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/prepare.ps1
python -B scripts/workflow.py register-current --audit-dir <audit-dir> --preflight-dir <receipt>
python -B scripts/workflow.py doctor --audit-dir <audit-dir> --preflight-dir <receipt>
python -B scripts/workflow.py identify --audit-dir <audit-dir>
python -B scripts/workflow.py models --workspace <job-dir> --audit-dir <audit-dir> --preflight-dir <receipt>
```

Use an actual discovered Gemini model for one simple tool-free task and verify the answer. This consumes model quota and must be within the user's authorized installation acceptance. Default acceptance does not rotate every account or run the full live matrix; test switching only when requested.

Skip enrollment if a matching compatible record already exists. Install missing Python/CLI dependencies from the sources above. For an empty store, `doctor` reports `ACCOUNT_STORE_EMPTY` and points to enrollment. Default operation requires no Tools service. Handle missing configuration, startup or authentication in Tools mode through that backend's diagnostics without closing user sessions.

## 5. Report the result

Report these separately:

- **Installed**: the destination is complete, skill entrypoint and language docs are readable, and source commit is recorded. Codex can use the skill on the next turn after installation.
- **Environment ready**: dependencies, account identity and valid preflight checks pass.
- **Live call verified**: one simple task returns and passes acceptance with the explicit model and effort.

Describe missing software, configuration, login or permissions alongside completed steps. Claim switching, fingerprint writes or genuinely exhausted-quota recovery only if actually performed with corresponding evidence.

Retain [LICENSE](../LICENSE) and [NOTICE.md](../NOTICE.md) when distributing this project. The project and adaptation use CC BY-NC-SA 4.0, including its noncommercial restriction.
