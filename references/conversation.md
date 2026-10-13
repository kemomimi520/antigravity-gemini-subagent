# Conversation continuation and checkpoints

Use native `--conversation <id>` in a new process after identity verification. The selected native account backend supplies credentials/profile; retained native conversation state supplies context. Builtin is the default and does not require the Tools desktop application. Do not copy credentials into conversations, edit conversation stores or select latest with `--continue`.

## Follow-up turns

Create a new task ID with new prompt/acceptance, explicit model, same authorized workspace and exact prior `conversation_id`. Each turn is separately audited. Reusing a successful task ID is rejected.

Returned identity must match. On conversation-load failure or a different conversation, preserve diagnostic and stop; no quota rotation or silent replay from scratch.

## Quota-failed task

`run` performs one call. Main agent coordinates refresh/rank/native-switch/identify/run. Preserve task bytes/ID. The helper uses the failed attempt's conversation ID, including init when terminal error omits it.

Tool-free `retry_safe: true` work may retry after verified alternate account. If no conversation was created, only such replay-safe work can restart from the original prompt. Failed explicit resume never silently falls back.

After tools ran or for non-replay-safe work, independently verify effects before creating a checkpoint:

```json
{
  "task_id": "original-task-id",
  "task_sha256": "hash from task state",
  "last_attempt_sha256": "workflow.attempt_digest(state['attempts'][-1])",
  "conversation_id": "ID from the failed attempt",
  "safe_to_resume": true,
  "completed_effects": ["checkpoint.json exists with independently verified content"],
  "verification": "Actual diff/content checks performed by the main agent",
  "remaining_work": "Only unfinished work; do not repeat completed effects"
}
```

Pass `--resume-review <review.json>` with original arguments. Review is bound to exact attempt/task/conversation and supplies completed/remaining work in the resumed prompt. It is a supervisor checkpoint within existing task authorization, not a permission bypass.

Review cannot override non-quota failure, retry failed accounts, change task/workspace or increase a saved task's budget by editing bytes. Partial effects without conversation ID cannot replay through this path. Select any larger positive `max_attempts` during task authoring; default is 3.

## Evidence and limits

Prior live acceptance retained random marker, Chinese text and numbers through same-account resume, unrelated intervening conversation and native account switch. Exact cross-account ID and subsequent file work passed; initial identity/selection were restored. This is observed tested-host behavior, not a promise for every future build.

`scripts/live_conversation.py` tests native continuation/checkpointed effects. Its healthy-account switch is not an actual exhaustion recovery event; offline quota fixtures do not replace one. Never consume quota deliberately to manufacture exhaustion.

[Native conversation documentation](https://antigravity.google/docs/cli/headless/#continue-a-conversation).
