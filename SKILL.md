---
name: antigravity-gemini-subagent
description: Delegate bounded Gemini tasks through native Antigravity CLI, resume explicit conversations, and recover verified exhausted account quota through Antigravity Tools. Use for supervised Gemini worker calls and native account continuation, not inference proxies or IDE UI automation.
---

# Antigravity Gemini Subagent

Use native `agy` as an external worker. It does not inherit the main agent's conversation. Supply the task, applicable rules, authorized inputs, workspace and acceptance criteria; independently verify its output. The helper performs one call at a time. The main agent coordinates quota recovery.

## Prepare

Read [environment.md](references/environment.md) for first use, missing dependencies or path overrides. The full workflow requires Windows, an authenticated eligible CLI account, existing Antigravity Tools accounts, and the managed preflight contract. Use the existing host preflight when present, otherwise the bundled fallback; neither copying nor invoking the skill transfers authorization to elevate.

Run the read-only `doctor` after installation changes. It inspects local configuration without starting applications; it does not prove live eligibility or refreshed quota. Discover models under a valid preflight receipt. Preserve the user's model and effort; choose a Gemini slug actually returned by the CLI. Check capabilities and postconditions instead of version allowlists.

```powershell
python <skill>/scripts/workflow.py doctor --audit-dir <audit-dir>
python <skill>/scripts/workflow.py models --workspace <job-dir> --audit-dir <audit-dir> --preflight-dir <receipt>
```

## Submit and verify

Create a UTF-8 task JSON in the authorized audit directory:

```json
{
  "task_id": "parser-review",
  "model": "gemini-<discovered-slug>",
  "effort": "high",
  "prompt": "Review the supplied synthetic parser. Give concrete failing inputs. Do not call tools.",
  "acceptance": "Actionable findings with examples; do not claim tests were run.",
  "read_only": true,
  "retry_safe": true,
  "max_attempts": 3
}
```

Replace the illustrative model before execution. Effort is `low`, `medium` or `high`; omitted effort follows a matching `low/medium/high` model suffix, otherwise defaults to `high`. `max_attempts` defaults to 3 and accepts an explicit positive integer; choose a finite budget appropriate to the authorized task. Unknown task fields are rejected. See [protocol.md](references/protocol.md) for fields and result semantics.

```powershell
python <skill>/scripts/workflow.py run --task <task.json> --workspace <job-dir> --audit-dir <audit-dir> --preflight-dir <receipt>
```

Use an isolated directory for tool-free reasoning. For file work, use the authorized project/worktree and absolute file paths in the prompt. Keep credentials, manager data and unrelated repositories out of worker inputs. `read_only` enables CLI plan mode and instructions; it is not an OS sandbox. Global rules and MCP configuration may load.

For authorized file tools, add `"allow_workspace_files": true`. The helper temporarily grants scoped workspace file access, preserving deny/ask priority. It restores settings when still owned; concurrent edits are preserved and reported. Inspect cleanup warnings before continuing. `--settings-file` must identify the settings actually loaded by that CLI; the override does not redirect native CLI configuration.

For machine-readable output, supply `json_schema`. Read `structured_output` and independently verify the required values. The helper checks its documented schema subset locally and rejects unsupported keywords before invocation; it does not implement all JSON Schema drafts.

Inspect status, conversation ID, tool errors/denials, identity evidence, response and actual file effects. Exit 0 means a valid worker response, not independent acceptance. `model_verified` checks the CLI init model against the requested slug; it does not identify the server's backend model. Do not infer experimental status or capability from model suffixes or knowledge questions without separate evidence.

## Continue a conversation

For a new turn, create a new task ID and add `conversation_id` from the previous result. Keep the explicit model and authorized workspace. The helper uses exact `--conversation` and verifies the returned ID; never use `--continue` to guess the latest session. Failed resume does not authorize silently starting over. Read [conversation.md](references/conversation.md) for checkpoints.

## Recover exhausted account quota

Start with the current verified CLI account. Rotate only after exit 20 / `QUOTA_EXHAUSTED` with account-quota evidence. Bare `RESOURCE_EXHAUSTED`, generic 429, capacity/overload, authentication/eligibility, timeout, network, permissions or malformed output do not authorize rotation.

1. Refresh all existing accounts and rank for the same model, excluding attempted accounts. Unknown, stale or blocked quota is not eligible; scoring uses the minimum relevant Gemini quota window.

   ```powershell
   python <skill>/scripts/workflow.py accounts --model <same-model> --refresh --start-manager --exclude <attempted-ref> --audit-dir <audit-dir> --preflight-dir <receipt>
   ```

2. Invoke native Antigravity Tools switching and verify actual CLI identity and explicit `agy` target.

   ```powershell
   python <skill>/scripts/workflow.py switch --account-ref <selected-ref> --start-manager --audit-dir <audit-dir> --preflight-dir <receipt>
   python <skill>/scripts/workflow.py identify --expect <selected-ref> --require-cli-target --audit-dir <audit-dir>
   ```

3. Rerun the unchanged task/workspace. The helper restores the failed attempt's exact conversation ID. Tool-free replay-safe work can retry; after tool effects or for non-replay-safe work, inspect completed effects and supply `--resume-review <review.json>` as described in [conversation.md](references/conversation.md). Existing authorization does not justify repeating completed effects.

Stop on no fresh eligible account, a non-quota failure, identity/switch failure, uncertain switch outcome or attempt limit. Do not retry failed accounts, add accounts, buy credits or change security/network settings to continue.

Read [account-switch.md](references/account-switch.md) before switching: native `agy` and Antigravity 2.0 share Windows credentials. Serialize the whole recovery workflow; a per-operation lock cannot prevent another GUI/session from changing credentials. Existing sessions may write cached credentials back. Do not terminate user sessions automatically.

The worker uses native CLI inference, not the manager's inference-proxy API. Network traffic may inherit HTTP proxy settings. Manager control requests remain on loopback with proxies/redirects disabled. Never send manager tokens or account records to Gemini.

## Test and report

Use [test-workflow.md](references/test-workflow.md) for offline checks and authorized live acceptance. Distinguish fixtures, real calls, native switches, explicit resumes and an actually exhausted account. Never deliberately consume quota to manufacture exhaustion. Retain failures and report untested branches.
