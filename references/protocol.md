# Task and result protocol

## Task fields

Tasks are UTF-8 JSON objects (BOM accepted). Duplicate keys/nonfinite numbers and unknown fields are rejected. Task state binds exact task bytes and workspace: even formatting changes need a new task ID unless restoring original bytes.

| Field | Requirement |
| --- | --- |
| `task_id` | Required, 1-64 ASCII letters/digits/`_`/`-` |
| `model` | Required explicit `gemini-...` slug discovered with `models` |
| `prompt` | Required nonblank task text and applicable rules |
| `acceptance` | Required nonblank criteria, independently checked by main agent |
| `read_only` | Required Boolean; plan mode/instructions, not an OS sandbox |
| `retry_safe` | Required Boolean; replay safety, not permission to repeat effects |
| `effort` | Optional `low/medium/high`; matching model suffix by default, otherwise `high` |
| `max_attempts` | Optional positive integer, default 3; finite attempts across invocations |
| `conversation_id` | Optional exact native ID; ASCII alphanumeric first, then letters/digits/`_`/`-`, at most 128 characters |
| `json_schema` | Optional supported schema object, checked before call |
| `allow_workspace_files` | Optional Boolean, temporary scoped file-tool grant |

Absolute authorized workspace is supplied through prompt, working directory and `--add-dir`. File prompts should use absolute paths. Global rules/MCP configuration are not isolated.

## Native CLI contract

Invocation uses explicit model/effort, `--print`, `--output-format stream-json`, finite `--print-timeout`, `--log-file` and no shell interpolation. Read-only calls use `--mode plan`. Do not combine it with `--disable-slash-commands`: observed CLI behavior disables the intended plan mode in that combination. No blanket permission bypass is used.

Success requires a valid terminal result, matching init model, consistent conversation, nonempty output and no unresolved tools/permissions. A later tool success recovers an error only with matching operation/target and completion evidence; another file/command's success cannot hide failure. Independently inspect output/diff.

`model_verified` is the CLI-selected slug. `runtime_model_verified` remains false: the generic helper does not decode server backend IDs. No official capability/experimental meaning is assigned to suffixes or knowledge answers.

Timeouts preserve available conversation/tool events and hashes. Caught process/supervisor failures finalize attempt state. These are non-quota outcomes: inspect effects and stop automatic replay. Abrupt termination or disk failure may still leave incomplete state; do not delete evidence to rerun.

## Structured output subset

The helper passes the schema to native `--json-schema` and locally validates successful `structured_output`.

Supported validation keywords:

- `type` (including type arrays), `enum`, `const`.
- `properties`, `required`, `additionalProperties`.
- Single-schema `items`, `minItems`, `maxItems`, `uniqueItems`.
- `minLength`, `maxLength`, restricted common-syntax `pattern`.
- `minimum`, `maximum`, numeric `exclusiveMinimum/exclusiveMaximum`, `multipleOf`.
- `allOf`, `anyOf`, `oneOf`, `not`.
- `$defs`, `definitions`, root-relative JSON Pointer `$ref`.

`title`, `description`, `$schema`, `default` and `examples` are annotations; defaults are not applied. `$schema` does not enable a complete draft implementation. Local references never fetch network resources. Anchors/external references are unsupported. Some regex escapes/extensions and quantified groups are rejected; this is not an untrusted-regex sandbox. Depth/node limits bound traversal.

Unsupported keywords such as `format`, `$id`, `prefixItems`, `patternProperties` or `unevaluatedProperties` fail before invocation. Schema validity does not prove factual correctness.

Saved/returned output is redacted. `response_redacted/structured_output_redacted` indicate changes; schema-breaking redaction makes the call unsuccessful. Do not include secrets in expected output to evade redaction.

## Results and stopping conditions

| Status/exit | Meaning |
| --- | --- |
| `SUCCESS` / 0 | Valid response; main-agent acceptance still required |
| `QUOTA_EXHAUSTED` / 20 | Account-quota evidence permits bounded recovery |
| Worker/tool/policy error / 21 | Non-quota failure; inspect diagnostic/effects |
| `PROTOCOL_ERROR` / 22 | Inconsistent model/conversation or invalid output |
| `TIMEOUT` / 23 | Partial evidence retained; no automatic replay |
| `BLOCKED` / 24 | Prerequisite/receipt/state guard failure |
| Identity failure / 25 | Shared identity changed/unverifiable for guarded call |
| No eligible quota / 26 | No fresh eligible candidate |
| `SWITCHED_CLEANUP_FAILED` / 27 | Verified switch committed; cleanup failed |
| `SWITCH_OUTCOME_UNKNOWN` / 28 | Submitted operation uncertain; do not race it |
| `AUDIT_CLEANUP_FAILED` / 29 | Native log may remain unsanitized |

A valid tool-free read-only answer may carry `SHARED_IDENTITY_CHANGED_DURING_RUN`; account attribution remains unverified. This is not permission to retry/rotate. Tool-using/write-capable/failed calls retain identity stopping guards.

Audit records hashes, event summaries, identity snapshots, command metadata and retry state. Redaction is best effort, not a publication guarantee. Keep logs/state/backups local.

## Manager protocol

Control uses configured loopback only, disabling inherited proxies/redirects; authentication remains in memory.

- `GET /api/accounts`: inventory/selection.
- `POST /api/accounts/refresh`: mutates local tokens/account state and contacts quota services.
- `POST /api/accounts/switch`: explicit `accountId` and `targetIde: agy`; native desktop integration, empty HTTP 200 accepted.

Ranking uses fresh exact-model quota and minimum applicable Gemini windows, not tier or average. Missing/nonfinite/ambiguous/stale quota is unknown. Refresh coverage/partial failures are checked. `proxy_disabled` affects proxy routing, not native eligibility. Cached inspection is labelled and never presented as refreshed acceptance.

See [account-switch.md](account-switch.md) for verification/rollback and [environment.md](environment.md) for receipts/startup ownership.

Primary sources: [CLI headless protocol](https://antigravity.google/docs/cli/headless/), [troubleshooting](https://antigravity.google/docs/cli/troubleshooting/), [manager integration](https://github.com/lbjlaq/Antigravity-Manager/blob/main/src-tauri/src/modules/integration.rs), [account service](https://github.com/lbjlaq/Antigravity-Manager/blob/main/src-tauri/src/modules/account.rs). Source inspection is not live acceptance or a version gate.
