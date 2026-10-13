# Antigravity Gemini Subagent

English | [简体中文](README.zh-CN.md)

**Delegate tasks from Codex to Gemini, then keep working in the same conversation.**

This skill brings the native Antigravity CLI into a Codex workflow. Ask Gemini for another code review, help with a reasoning problem, or file work in a chosen project. Codex checks the result and brings it back into your task.

If an account runs out of quota partway through, Codex can use the bundled account core to switch to another existing account, verify the switch, and resume the original Gemini conversation.

## Features

| Feature | How it helps |
| --- | --- |
| Task delegation | Give Gemini a task, context, workspace and acceptance criteria |
| Model and effort selection | Discover available models and choose `low`, `medium` or `high` effort |
| Files and structured output | Work in a chosen directory or request a JSON schema result |
| Follow-up conversations | Keep the conversation ID for questions, new constraints and unfinished work |
| Account continuation | Resume the same conversation after a verified native switch for exhausted quota |

Use it for a focused part of a larger task or an independent check of a conclusion. Codex prepares the context sent to Gemini and reviews what comes back.

## Quick start

### 1. Prepare your environment

The complete workflow currently supports **Windows**. Install Python and the native [Antigravity CLI](https://antigravity.google/docs/cli/getting-started/), with an account already signed in and eligible for your chosen model. Account operations are bundled, so Antigravity Tools installation is optional.

Runtime helpers use only the Python standard library; Python 3.11 has been tested. Models are discovered from the CLI, with no daily-version allowlist.

### 2. Install the skill

**Option A: ask your model to install it**

If your model can access the web and operate on local files, send it this request:

```text
Install this skill and any missing software dependencies:
https://github.com/kemomimi520/antigravity-gemini-subagent
Read references/installation.md first and follow its installation, dependency-check and acceptance steps.
Ask me to complete account sign-in when needed. Preserve existing software and accounts.
```

The [installation guide](references/installation.md) specifies the destination, missing-dependency handling and acceptance criteria. The model needs appropriate local permissions. A private repository also requires existing GitHub access.

**Option B: install manually**

Download or clone this repository into your Codex skills directory. Keep the complete folder and its name, `antigravity-gemini-subagent`. The default Windows location is:

```text
%USERPROFILE%\.codex\skills\antigravity-gemini-subagent
```

First use requires a local preflight and backup, including administrator rights and a system restore point. See [environment setup](references/environment.md) for custom paths and preparation steps.

### 3. Use it in Codex

For example, ask Gemini to review code:

```text
Use $antigravity-gemini-subagent to review src/parser.py in this project.
Choose a currently available Gemini model with high effort.
Read files only. Focus on edge cases and give concrete inputs and fixes.
Check its findings before reporting them to me.
```

Then follow up:

```text
Continue that Gemini subagent conversation and ask for test cases
for the issues we confirmed.
```

Codex prepares the task, invokes the CLI, saves the conversation ID and checks the result. You can also name a specific currently available model in your request.

<details>
<summary>Call the scripts directly</summary>

Run from the skill directory. Replace angle-bracket placeholders with your actual paths:

```powershell
python scripts/workflow.py doctor --audit-dir <audit-dir>
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/prepare.ps1
python scripts/workflow.py models --workspace <job-dir> --audit-dir <audit-dir> --preflight-dir <receipt>
```

`receipt` is the directory produced by a successful preflight. Before preparation, `doctor` may report `NOT_READY` because that record is missing.

Save this task as UTF-8 `task.json`, replacing `model` with an actual discovered model name:

```json
{
  "task_id": "copy-explanation",
  "model": "gemini-REPLACE_WITH_DISCOVERED_MODEL",
  "effort": "medium",
  "prompt": "Explain shallow and deep copies in Python using a nested list example. Do not use tools.",
  "acceptance": "Accurate explanation; example shows the results after a nested element changes; no tool calls.",
  "read_only": true,
  "retry_safe": true
}
```

```powershell
python scripts/workflow.py run --task <task.json> --workspace <job-dir> --audit-dir <audit-dir> --preflight-dir <receipt>
```

See the [task protocol](references/protocol.md) for options, JSON schemas and result fields.

</details>

## Do I still need Antigravity Tools?

**Not by default.** The relevant Antigravity Tools account logic has been adapted into a Python module for local enrollment, OAuth refresh, quota queries, and synchronization of Windows credentials, CLI credential files and applicable device profiles.

If you have no Tools installation, sign in through the native CLI and run `register-current` once to enroll that account. Existing Tools account data can also be reused. The default account directory remains `.antigravity_tools`, but the builtin backend needs no GUI configuration, manager executable or local management service. See the [installation guide](references/installation.md).

The desktop manager remains an optional backend: `--account-backend tools`. Features not included in the port, such as its independent proxy-pool account bindings, require that backend. The builtin core rejects unsupported network bindings rather than silently choosing another route. See [bundled account operations](references/native-accounts.md).

## How it works

Native `agy` sends model requests to Gemini. The bundled core handles account operations by default. **This project does not use an inference-proxy API.** CLI and account requests can inherit your existing HTTP network proxy.

The account core adapts Antigravity Tools' credential formats, device fields and switching sequence, followed by identity and applicable storage checks. Conversations resume with an explicit `conversation_id`. If a previous call changed files, Codex checks completed effects before continuing so that work is not repeated.

Codex coordinates quota recovery: confirm exhaustion → refresh existing account quotas → switch natively and verify → resume the original conversation. Network errors, service overload and authentication failures do not trigger switching. See [account switching](references/account-switch.md) and [conversation continuation](references/conversation.md).

## Validation and limits

Live Windows checks cover Gemini calls, model and effort selection, structured output, file work, and continuation across a native account switch. Bundled-core acceptance also verifies fresh-store enrollment, direct quota refresh for 8 accounts, and switching/continuation without calling a Tools service; original credentials and file contents were restored afterward. GitHub CI runs synthetic offline regressions and source checks without real accounts.

The complete recovery path triggered by genuinely exhausted quota has not been live-tested. Legacy fingerprint-store writes have synthetic and source-review coverage only. Model verification checks the CLI-selected name, not the server's internal model identifier. See [test coverage](references/test-workflow.md).

Native CLI and Antigravity 2.0 share Windows account credentials, so account switches need coordination with other sessions. Read-only mode uses CLI plan mode rather than OS-level isolation. Review task output, and keep logs and backups local; they may contain sensitive data.

## Documentation and development

- [Skill instructions](SKILL.md): how Codex uses the worker.
- [Installation guide](references/installation.md): model-assisted setup, dependencies and acceptance.
- [Environment setup](references/environment.md): dependencies, paths, network and preflight.
- [Task protocol](references/protocol.md): inputs, results, tool permissions and failure states.
- [Account switching](references/account-switch.md): credentials, device profiles, checks and recovery.
- [Bundled account operations](references/native-accounts.md): enrollment without the desktop tool and backend selection.
- [Conversation continuation](references/conversation.md): follow-ups and file checkpoints.
- [Test workflow](references/test-workflow.md): offline checks and live acceptance.

Run offline checks:

```powershell
python -B -m unittest discover -s tests -v
python -B scripts/package_skill.py
```

Commit source, documentation and synthetic tests only. Keep account credentials, task logs, conversations and rollback backups out of the repository. The source checker covers an explicit file allowlist and common secret patterns.

## License

This project uses [CC BY-NC-SA 4.0](LICENSE): attribution, noncommercial use and share-alike. The bundled account core derives from [Antigravity Tools](https://github.com/lbjlaq/Antigravity-Manager); see [NOTICE.md](NOTICE.md) for source attribution and modifications. Commercial use requires appropriate additional permission from the relevant rights holders.
