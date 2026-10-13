# Attribution and modifications

This project is licensed under Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International (CC BY-NC-SA 4.0). See [LICENSE](LICENSE).

The bundled account core in [scripts/native_accounts.py](scripts/native_accounts.py) is a Python adaptation of selected Antigravity Tools implementation by **lbjlaq and the Antigravity-Manager contributors**:

- Upstream: https://github.com/lbjlaq/Antigravity-Manager
- Source snapshot: `b601f5e7375343343922ccab5e50653bc3450c3b`
- Upstream license: https://github.com/lbjlaq/Antigravity-Manager/blob/b601f5e7375343343922ccab5e50653bc3450c3b/LICENSE
- Relevant source: `src-tauri/src/modules/account.rs`, `oauth.rs`, `quota.rs`, `device.rs`, `integration.rs`, and `src-tauri/src/models/account.rs`, `token.rs`, `quota.rs`.

Changes made on 2026-10-13: ported the needed account store, OAuth refresh, quota parsing, device-profile handling and Windows/CLI credential integration to Python's standard library; replaced the desktop application's service/GUI lifecycle with a bundled local backend; added current-CLI enrollment, strict parsing, private-file protection and supervisor-owned verification/recovery. Existing native CLI inference and explicit conversation continuation are retained.

Upstream-compatible account files can be used without installing the Antigravity Tools application. This is a scoped adaptation, not a copy of its full desktop application, inference proxy, UI, auto-updater or other IDE integrations. Third-party branding does not imply upstream endorsement.

The public OAuth client identifier and client secret embedded in the adaptation are the upstream desktop application's published constants, not user account tokens. User account data, credentials, logs and backups are excluded from distribution.

Attribution, modification notices, the noncommercial condition and share-alike terms must accompany applicable redistributed adaptations. The material is provided as-is, subject to the warranty disclaimer and other terms in LICENSE. Commercial use requires appropriate additional permission from the relevant rights holders.
