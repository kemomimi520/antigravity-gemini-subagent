# Bundled native account core / 内置账号模块

The default `--account-backend builtin` uses [native_accounts.py](../scripts/native_accounts.py). It is a scoped Python adaptation of Antigravity Tools, licensed under CC BY-NC-SA 4.0; see [NOTICE.md](../NOTICE.md) and [LICENSE](../LICENSE). The upstream source commit records provenance, not a runtime version requirement.

默认内置后端，无需安装、启动或调用 Antigravity Tools。Python 标准库直接处理本地账号文件和 Google 账号接口；模型推理仍使用原生 `agy`。新装用户先在 CLI 登录，再在有效预检下登记当前账号：

```powershell
python scripts/workflow.py register-current --audit-dir <audit-dir> --preflight-dir <receipt>
```

## Accounts and enrollment / 账号与登记

The default directory is `~/.antigravity_tools` for upstream data compatibility. Override it with `--account-dir` (`--manager-dir` remains an alias) or `AGY_SUBAGENT_MANAGER_DIR`. Existing `accounts.json` and `accounts/<id>.json` are reused without a GUI config or service. A fresh directory can be initialized from the already signed-in native Windows credential; Google userinfo verifies the account email before saving. This does not create a Google account or perform OAuth consent for the user.

默认目录保留上游命名，是数据兼容选择，不代表依赖桌面应用。多个账号可由用户依次在原生 CLI 登录并执行登记；同一已验证邮箱重新授权时保留原账号 ID 和绑定配置。登记过程不把凭证交给模型，输出只含脱敏账号引用。新私有文件在写入凭证前设置受保护的访问控制；已有文件替换保留其 DACL。

## Scope / 移植范围

- Upstream-compatible account/index metadata and bound device profiles.
- OAuth refresh with the upstream public client constants and optional `ANTIGRAVITY_OAUTH_CLIENTS` / `ANTIGRAVITY_OAUTH_CLIENT_KEY` configuration. The original refresh token, optional ID token and account metadata are retained.
- Google `loadCodeAssist`, `fetchAvailableModels` and `retrieveUserQuotaSummary` account requests, with fixed HTTPS origins, no redirects and conservative fresh-quota validation. These are account operations, not model inference.
- Same-family CLI effort names can use their corresponding upstream `-tiered` quota entry. Exact names take priority; ambiguous/cross-family matches are rejected, protections apply to both names, and the requested inference model stays unchanged.
- Windows `gemini:antigravity` credentials plus native/fallback CLI credential files, exact identity readback, applicable classic fingerprint JSON fields and the SQLite `storage.serviceMachineId` row.
- Supervisor-controlled switching/recovery and explicit conversation continuation. Failures preserve concurrent changes and report incomplete restoration rather than claiming success.

设备绑定生成格式、已有绑定的保留，以及 Windows/文件凭证同步均对应上游相关实现。只处理实际存在的旧版指纹存储；没有存储时，不创建一个假的旧版安装。设备 JSON 和 SQLite 分支有合成覆盖；当前真实主机缺少旧版存储，实际写入仍未实测。

## Optional Tools backend / 可选桌面工具后端

Use `--account-backend tools` to retain the previous native Tools API workflow. That backend needs an installed/configured manager and its management service for refresh/switching. `--start-manager` applies only to Tools; the builtin backend starts no desktop manager.

The port excludes the desktop GUI, tray, updater, inference proxy, other IDE targets and account-specific proxy-pool dispatch. An account with `proxy_id` configured is rejected by builtin refresh/switch/enrollment with `ACCOUNT_BOUND_PROXY_REQUIRES_TOOLS_BACKEND`, preserving that route. Use Tools for that account rather than silently dropping its network binding. Existing global HTTP/HTTPS proxy settings can be used by the builtin account transport without changing system settings. SOCKS-only transport configuration requires an explicit supported HTTP/HTTPS route; it is not silently bypassed.

CLI and Antigravity 2.0 still share the Windows credential. A builtin switch does not notify or restart the Tools GUI, and cannot prevent external sessions from writing cached state back. Coordinate the complete workflow and verify identity; do not promise independent accounts per worker.
