# Antigravity Gemini Subagent

A Codex skill for supervising bounded Gemini tasks through native Antigravity CLI (`agy`), with exact conversation continuation and verified native account switching through Antigravity Tools.

The main agent supplies tasks and independently verifies answers. Python handles CLI invocation, protocol checks, identity, bounded retry state and native-switch postconditions. The worker does not inherit the main conversation. Quota recovery is a supervised sequence, not a background account rotator.

## Supported behavior

- Explicit discovered Gemini model and `low/medium/high` effort.
- Streaming result capture, tool/permission checks, identity evidence and redacted audit records.
- Exact conversation-ID continuation across a native account switch.
- Existing-account quota refresh/ranking and recovery only for verified account-quota exhaustion.
- Scoped temporary file permissions and supervisor-reviewed checkpoints after partial effects.

Inference uses native CLI, not manager inference-proxy APIs. Existing HTTP proxies may carry CLI traffic. No daily-version whitelist is used.

## Requirements and installation

**Windows is the supported full-workflow platform.** Python, an already authenticated eligible `agy`, installed Antigravity Tools with existing accounts, and the managed preflight provider are required. Runtime/offline tests use Python standard library; Python 3.11 has been tested.

Place the complete folder, including `SKILL.md`, `agents`, `scripts` and `references`, in the receiving agent's configured skills directory. Preserve folder name `antigravity-gemini-subagent`. Read [environment requirements](references/environment.md) before live use.

`prepare.ps1` uses the existing host managed preflight when present, otherwise the bundled `managed_preflight.ps1`. Administrator rights and a created/recent restore point remain required. The fallback cannot bypass a failed host preflight. Copying the skill does not authorize elevation, create credentials or enable unavailable models. Do not fabricate a receipt.

From this folder, inspect prerequisites without launching applications:

```powershell
python scripts/workflow.py doctor --audit-dir <authorized-audit-dir>
python scripts/workflow.py identify --audit-dir <authorized-audit-dir>
```

Doctor without a successful receipt reports the missing preflight as `NOT_READY`; this is expected before preparation. After authorized preparation, rerun it with `--preflight-dir <successful-receipt>`.

| Purpose | Flag | Environment variable |
| --- | --- | --- |
| Manager data | `--manager-dir` | `AGY_SUBAGENT_MANAGER_DIR` |
| Manager executable | `--manager-cli` | `AGY_SUBAGENT_MANAGER_CLI` |
| CLI executable (`models/run/doctor`) | `--cli` | `AGY_SUBAGENT_CLI` |
| Permission settings (`run/doctor`) | `--settings-file` | `AGY_SUBAGENT_SETTINGS` |

Settings override must point to the file the CLI actually loads; it does not redirect native configuration. Pass custom data/settings paths to `prepare.ps1` through `-ManagerDir/-SettingsFile` for backup coverage. [Environment](references/environment.md) explains defaults and discovery.

## First bounded task

After authorized preflight succeeds, discover models:

```powershell
python scripts/workflow.py models --workspace <authorized-job-dir> --audit-dir <authorized-audit-dir> --preflight-dir <successful-receipt>
```

Save UTF-8 task JSON. Replace the illustrative model with a discovered Gemini slug; example content is synthetic:

```json
{
  "task_id": "sum-example",
  "model": "gemini-<discovered-slug>",
  "effort": "medium",
  "prompt": "Without tools calculate 17 + 25 and return the requested JSON object.",
  "acceptance": "structured_output equals {\"sum\":42}; no tools",
  "read_only": true,
  "retry_safe": true,
  "max_attempts": 3,
  "json_schema": {
    "type": "object",
    "properties": {"sum": {"type": "integer"}},
    "required": ["sum"],
    "additionalProperties": false
  }
}
```

```powershell
python scripts/workflow.py run --task <task.json> --workspace <authorized-job-dir> --audit-dir <authorized-audit-dir> --preflight-dir <successful-receipt>
```

Independently verify `structured_output` equals `{"sum":42}`. The acceptance string guides the worker; arbitrary prose acceptance cannot be automatically judged by the helper. Exit 0/schema validity does not prove correctness.

For a follow-up, create a new task ID and include the prior `conversation_id`. The helper uses exact native `--conversation`, never guesses the latest session. See [protocol](references/protocol.md) and [checkpoints](references/conversation.md).

## Recovery and account effects

After confirmed `QUOTA_EXHAUSTED`, the main agent refreshes all existing accounts, selects fresh quota for the same model, performs native `agy` switching, verifies identity and resumes. `max_attempts` defaults to 3; explicit positive integers allow a larger finite budget. Failed accounts are not retried. Generic 429/`RESOURCE_EXHAUSTED`, overload, eligibility/authentication, network or timeout failures do not authorize rotation.

Read [native switching/rollback](references/account-switch.md) before use. Native CLI and Antigravity 2.0 share Windows credentials. The per-operation lock does not stop external sessions/apps from changing identity; serialize the complete workflow. Unknown switch outcomes are retained for inspection. No UI clicks, account creation, purchases or model downgrades are used.

`read_only` is CLI plan mode, **not an OS security boundary**. Global rules/MCP configuration may load. Only pass authorized data. Inspect real file diffs before resuming partial effects.

## Tests and evidence

```powershell
python -B -m unittest discover -s tests -v
```

Offline tests use synthetic state and need no real account credentials. CI runs offline checks only. Live scripts need authorization, valid preflight and model-call quota. [Test workflow](references/test-workflow.md) separates these cases.

Prior live acceptance established worker responses, native account switching, exact-ID cross-account continuation and checkpointed file work on the tested Windows host. That host had no legacy fingerprint/database stores, so their actual writes were not live-verified. A truly exhausted account has not triggered the entire live recovery chain. Do not claim complete live coverage or manufacture exhaustion.

`model_verified` means CLI init matched the requested slug, not that the server backend identifier matched. A prior diagnostic model probe is not a portable production feature. Model suffixes/knowledge questions do not establish official model capability.

## Publishing and privacy

Publish source/docs/synthetic tests only. Keep tasks, outputs, logs, account inventories, conversation stores, credentials and preflight rollback bundles outside source control. `.gitignore` prevents some accidental additions; it does not scrub secrets or undo commits. Review the package/staged content before publishing. Run `python scripts/package_skill.py --root <skill>` for a source/secret-pattern check. Add `--output <new-zip-outside-source>` to create a source-only ZIP; existing packages are not overwritten. This scan does not read private account directories or certify the absence of every secret.

Redaction recognizes known token/email patterns; it cannot guarantee removal of arbitrary secrets, private paths or task data. Native logs can contain sensitive content before postprocessing; abrupt termination may leave unsanitized records. Backups intentionally preserve full state including credentials. Never upload them. For a published secret, revoke/rotate it and follow [GitHub removal guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).

Helper replacements copy and verify the existing file's Windows DACL and inheritance-protection state before writing private contents. A failed security copy stops the write; it does not relax access. This preserves DACLs, not every file attribute, owner or SACL, and does not control writes performed internally by the native CLI or manager.

The repository license has not been selected. Choose/add a license before public release; this README implies no license grant.

Primary references: [CLI headless documentation](https://antigravity.google/docs/cli/headless/) and [native manager integration](https://github.com/lbjlaq/Antigravity-Manager/blob/main/src-tauri/src/modules/integration.rs).
