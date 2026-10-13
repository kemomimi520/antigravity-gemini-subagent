# Test workflow

## Offline acceptance

Run in authorized workspace; synthetic temporary files are created below current working directory.

```powershell
python -B -m unittest discover -s <skill>/tests -v
python <skill-creator>/scripts/quick_validate.py <skill>
python <skill>/scripts/package_skill.py --root <skill>
```

Validator checks frontmatter/packaging; tests check behavior; package scan checks public-source allowlist/common secret patterns. None proves live calls or absence of all secrets. CI runs offline checks only and must not receive live tokens.

Read-only diagnostics:

```powershell
python <skill>/scripts/workflow.py doctor --audit-dir <audit-dir>
python <skill>/scripts/workflow.py identify --audit-dir <audit-dir>
python <skill>/scripts/workflow.py accounts --model <chosen-gemini-slug> --audit-dir <audit-dir>
```

Without successful receipt, doctor may complete local checks but return `NOT_READY` for live operation. Cached ranking is labelled and does not prove refreshed quota/eligibility.

Meaningful negative cases: stale/future/unknown/NaN quota; exhausted window despite high model percentage; blocked/protected accounts; partial refresh failures; redirect/proxy credential exposure; malformed/duplicate terminal events; wrong model/conversation; empty output; unsupported/invalid schemas; unknown fields; different-target tool success after error; generic 429/bare `RESOURCE_EXHAUSTED`/capacity/auth; timeout with partial effects; changed task/workspace; failed-account retry; concurrent worker; failed/missing preflight; uncertain rollback/concurrent edits.

## Authorized live acceptance

Do not install/upgrade software or deliberately exhaust quota. Follow host preflight/elevation rules; preserve starting identity/selection/settings.

1. Diagnose and preflight actual account/settings paths. Default to builtin; Tools mode must be explicit. Discover models and retain requested effort, without version gate.
2. Run isolated tool-free exact token task: `Return exactly GEMINI_WORKER_OK_437. Do not call tools.` Independently verify token, init model, identity, conversation and zero tools.
3. Run bounded synthetic reasoning/schema cases; independently verify values.
4. For authorized file cases, inspect real file content/diff and settings restoration. Permission denial is incomplete; plan mode is not isolation.
5. Refresh existing accounts when authorized. Check coverage, freshness/partial failures, minimum windows and owned-process/config cleanup.
6. If switching is authorized, native-switch to a candidate, verify full credentials/profile and make a smoke call. Restore baseline as required. Absent legacy stores are not performed writes.
7. Resume exact conversation across switch and unrelated conversation; verify remembered synthetic context/ID/checkpointed file effects. Restore baseline.

Report distinct statuses for offline checks, diagnosis, model discovery, real response, refresh, native switch, exact resume, fingerprint write and **actual quota-triggered recovery**. Mark unrun/blocked cases; healthy switch/resume is not exhaustion.

Use `scripts/live_matrix.py --workspace <job-dir> --audit-dir <audit-dir> --preflight-dir <receipt>` for bounded live coverage. `--all-models` tests discovered Gemini slugs; `--include-tools` adds authorized synthetic file/harmless command cases. `--tools-only` is for relevant new tool changes. Inspect each script's `--help` for path overrides. Do not repeat every passing live case without new changes/failures.

Use `scripts/live_conversation.py --model <discovered-slug> --workspace <job-dir> --audit-dir <audit-dir> --preflight-dir <receipt>` for authorized continuation.

Prior evidence established real response, native switch/return, exact-ID cross-account resume and checkpointed file work. No actual legacy fingerprint/database write occurred because stores were absent. Complete live recovery after genuine exhaustion remains unobserved. Keep runtime evidence local and public fixtures synthetic.

On 2026-10-13, bundled-core acceptance established current-CLI enrollment in a fresh account store with no GUI config, a real tool-free structured Gemini call, direct quota refresh for 8 existing accounts, same-family tiered quota ranking, native alternate-account switch, explicit-ID cross-account context/schema/file continuation, and return to baseline. The manager executable override pointed to an absent path, and no Tools service was used. Exact original Windows credential and existing credential/settings file bytes were restored. Private live fixtures and evidence are excluded from distribution. The test host still had Tools installed; it was neither started/called by the builtin workflow nor uninstalled to manufacture a clean machine.

The first model-discovery attempt was blocked because the harness had not created its job directory; it issued no model request. Initial fresh quota data also exposed a CLI effort-name versus upstream `-tiered` name mismatch. Both failures were retained; the harness directory and same-family quota lookup were corrected and subsequently verified. Neither condition was treated as exhausted account quota. Legacy fingerprint stores were absent, and actual exhausted-quota-triggered recovery remains unobserved.
