# Native account switching

Use the bundled native account core by default, or explicitly select Antigravity Tools with `--account-backend tools`. Both apply the native `agy` credential/file/device integration, without UI automation or inference proxy calls. Read [native-accounts.md](native-accounts.md) for the bundled scope and licensing.

## Switch contract

The builtin core executes the adapted switching logic locally, using Windows credential APIs and upstream-compatible files. No manager service is involved. Tools mode sends `POST /api/accounts/switch` with camelCase `{"accountId":"<local-id>","targetIde":"agy"}`; native success returns empty HTTP 200. Management authentication goes only to configured loopback, with proxies/redirects disabled. The helper always selects the CLI target.

The selected backend refreshes/prepares tokens, handles applicable project readiness, retains an existing bound device profile or binds one when absent, then applies native integration. The `agy` path writes shared Windows credential `gemini:antigravity`, synchronizes fallback/native CLI file credentials, applies the profile to relevant existing stores and updates selection. It does not restart IDE sessions. Tools' headless integration cannot perform host-keyring switching; builtin does not need a desktop manager to access native Windows credentials.

In Tools mode, authorized `--start-manager` starts headless mode for refresh or native desktop hidden/`--minimized` for switching. Builtin starts no manager. Existing managers are reused and left running. Do not kill user-owned headless managers to replace them if they reject switching.

## Verification and owned recovery

Preserve prior complete Windows credential, manager index/account files, fallback credentials and effective fingerprint state in supervisor memory. Managed preflight must back up affected paths.

Verify after switching:

- Unique actual CLI identity, explicit target and manager selection.
- Full Windows credential payload, including optional `id_token` only when matching the local account.
- Fallback access/refresh token and expiry; native `.gemini/antigravity-cli/antigravity-oauth-token` when present. Timestamp units may be seconds/milliseconds.
- Preserved account/index metadata and unchanged existing bound profile.
- Effective legacy fingerprint fields/database row when present.

Native fingerprint path priority is configured user-data-dir, active classic process, portable installation, standard classic storage. Explicit CLI target does not fall back to IDE profile. Ambiguous installations stop. Absence is `no_legacy_storage_present`, not a successful write.

After a failed builtin switch, restore exact prior credential, files and the relevant fingerprint SQLite row only while they still match that operation's recorded writes. Tools restoration uses the native manager action first, then exact still-owned prior state. Win32 writes are part of the adapted builtin integration and its recovery, and remain restoration-only in Tools mode. Preserve unrelated rows/concurrent edits. Do not delete new non-workspace files to conceal partial operations.

A Tools timeout/disconnect may leave switching in progress. Preserve the owned process and inspect identity. HTTP 409 does not authorize rollback over another switch. Builtin has no asynchronous manager switch; caught failures restore only its recorded effects. Abrupt termination still requires state inspection. Concurrent changes are preserved and reported as incomplete recovery.

Exit 27 / `SWITCHED_CLEANUP_FAILED` means the verified switch committed before outer cleanup failed. Inspect `completed_operation` and identity before retry. Restore startup config only when its rewrite is known/owned; stop only the helper-owned process after completed operations.

## Shared-state limit

CLI and Antigravity 2.0 share Windows credentials; older sessions may write cached credentials back. Per-operation locking does not isolate external apps or the whole multi-command recovery transaction. Coordinate it, record other sessions and verify identity. Do not terminate user sessions automatically. `proxy_disabled` does not disable native application usage.

Switch/refresh success does not prove model eligibility. Preserve eligibility diagnostics as non-quota failures; wording alone does not establish geography or a fingerprint defect.

## Live evidence

Acceptance on 2026-10-12 established alternate-account switch/return, full OS/file readback, retained bound profile and a successful explicitly selected Gemini call. Baseline identity/selection were restored. This identifies CLI selection, not independently decoded backend model.

The host lacked legacy fingerprint stores/databases; those branches have synthetic/source coverage, not live writes. True quota exhaustion has not triggered the complete recovery chain. Dates/source tags are not version gates.

Primary sources: [integration](https://github.com/lbjlaq/Antigravity-Manager/blob/main/src-tauri/src/modules/integration.rs), [account service](https://github.com/lbjlaq/Antigravity-Manager/blob/main/src-tauri/src/modules/account.rs), [device profiles](https://github.com/lbjlaq/Antigravity-Manager/blob/main/src-tauri/src/modules/device.rs), [account model](https://github.com/lbjlaq/Antigravity-Manager/blob/main/src-tauri/src/models/account.rs). Recheck capabilities/postconditions after updates.
