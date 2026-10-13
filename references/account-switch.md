# Native account switching

Use Antigravity Tools' native one-click backend, without UI automation or inference proxy calls.

## Switch contract

Send `POST /api/accounts/switch` with camelCase `{"accountId":"<local-id>","targetIde":"agy"}`. Native success returns empty HTTP 200. Management authentication goes only to configured loopback, with proxies/redirects disabled. The helper always specifies the CLI target.

The service refreshes/prepares tokens, handles applicable project readiness, retains an existing bound device profile or binds one when absent, then invokes desktop integration. The `agy` branch writes shared Windows credential `gemini:antigravity`, synchronizes fallback/native CLI file credentials, applies the profile to relevant existing stores and updates selection. It returns before IDE restart. Headless integration cannot perform host-keyring switching; do not substitute a credential-only action.

Authorized `--start-manager` starts headless mode for refresh or native desktop hidden/`--minimized` for switching. Existing managers are reused and left running. Do not kill user-owned headless managers to replace them if they reject switching.

## Verification and owned recovery

Preserve prior complete Windows credential, manager index/account files, fallback credentials and effective fingerprint state in supervisor memory. Managed preflight must back up affected paths.

Verify after switching:

- Unique actual CLI identity, explicit target and manager selection.
- Full Windows credential payload, including optional `id_token` only when matching the local account.
- Fallback access/refresh token and expiry; native `.gemini/antigravity-cli/antigravity-oauth-token` when present. Timestamp units may be seconds/milliseconds.
- Preserved account/index metadata and unchanged existing bound profile.
- Effective legacy fingerprint fields/database row when present.

Native fingerprint path priority is configured user-data-dir, active classic process, portable installation, standard classic storage. Explicit CLI target does not fall back to IDE profile. Ambiguous installations stop. Absence is `no_legacy_storage_present`, not a successful write.

After a **failed completed** switch, restore manager state natively first, then exact prior files/complete Windows credential when still owned. Manual Win32 writes are restoration only. Restore only the fingerprint SQLite row, preserving unrelated rows/concurrent edits. Do not delete new non-workspace files to conceal partial operations.

A timeout/disconnect may leave switching in progress. Preserve the owned process and inspect identity. HTTP 409 does not authorize rollback over another switch. Uncertain/conflicting native restoration stops before manual writes. Concurrent changes are preserved and reported as incomplete recovery.

Exit 27 / `SWITCHED_CLEANUP_FAILED` means the verified switch committed before outer cleanup failed. Inspect `completed_operation` and identity before retry. Restore startup config only when its rewrite is known/owned; stop only the helper-owned process after completed operations.

## Shared-state limit

CLI and Antigravity 2.0 share Windows credentials; older sessions may write cached credentials back. Per-operation locking does not isolate external apps or the whole multi-command recovery transaction. Coordinate it, record other sessions and verify identity. Do not terminate user sessions automatically. `proxy_disabled` does not disable native application usage.

Switch/refresh success does not prove model eligibility. Preserve eligibility diagnostics as non-quota failures; wording alone does not establish geography or a fingerprint defect.

## Live evidence

Acceptance on 2026-10-12 established alternate-account switch/return, full OS/file readback, retained bound profile and a successful explicitly selected Gemini call. Baseline identity/selection were restored. This identifies CLI selection, not independently decoded backend model.

The host lacked legacy fingerprint stores/databases; those branches have synthetic/source coverage, not live writes. True quota exhaustion has not triggered the complete recovery chain. Dates/source tags are not version gates.

Primary sources: [integration](https://github.com/lbjlaq/Antigravity-Manager/blob/main/src-tauri/src/modules/integration.rs), [account service](https://github.com/lbjlaq/Antigravity-Manager/blob/main/src-tauri/src/modules/account.rs), [device profiles](https://github.com/lbjlaq/Antigravity-Manager/blob/main/src-tauri/src/modules/device.rs), [account model](https://github.com/lbjlaq/Antigravity-Manager/blob/main/src-tauri/src/models/account.rs). Recheck capabilities/postconditions after updates.
