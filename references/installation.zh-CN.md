# 安装规范

[English](installation.md) | 简体中文

这份说明供安装模型执行，也供用户手动参考。目标是将 skill 安装到当前代理能发现的位置，并明确报告这台电脑是否能运行真实任务。

## 1. 检查现有环境

先读取 [环境配置](environment.md)，检查平台、Python、原生 `agy` 和已有账号状态。默认使用内置账号模块，无需安装 Antigravity Tools。完整运行流程支持 Windows；其他平台可以阅读源码和运行适用的离线测试。

| 依赖 | 用途 | 缺失时的处理 |
| --- | --- | --- |
| Python | 运行辅助脚本，使用标准库 | 使用当前可用的 Python 发行版；已测试 3.11，不锁定补丁版本 |
| Antigravity CLI | 原生模型调用与对话续接 | 按 [Google 安装说明](https://antigravity.google/docs/getting-started?tab=cli)安装，用户完成登录 |
| 内置账号模块 | 账号身份校验、额度刷新、原生换号 | 随本仓库安装；已有兼容数据可复用，否则在 CLI 登录后登记当前账号 |
| Antigravity Tools（可选） | 原来的桌面管理后端、尚未移植的代理池绑定 | 仅显式选择 `--account-backend tools` 时需要；按[上游安装说明](https://github.com/lbjlaq/Antigravity-Manager#installation)处理 |
| 有效的本地预检记录 | 真实调用前的备份与还原点检查 | 使用本 skill 的 `scripts/prepare.ps1`，遵循当前主机的授权和预检规则 |

新用户无需创建 Tools 的 GUI 配置或管理密钥。默认账号目录沿用 `.antigravity_tools`，只为兼容已有数据；空目录使用 `register-current` 从已登录 CLI 登记，不通过聊天粘贴或传递账号 token。移植范围和多账号登记见[内置账号模块](native-accounts.md)。

发现已有软件时保留安装和账号配置，按能力检查兼容性，不强制匹配某个版本号。自定义路径使用 [environment.md](environment.md) 中的参数或环境变量。

## 2. 安装 skill 文件

读取仓库根目录的 [SKILL.md](../SKILL.md) 和相关脚本后，获取仓库源码，记录实际分支及提交 ID。本仓库根目录就是 skill 根目录，安装名称为 `antigravity-gemini-subagent`。

尊重用户指定的安装位置。Codex 默认使用 `$CODEX_HOME/skills`；未设置 `CODEX_HOME` 时使用 `~/.codex/skills`。其他代理使用它自身支持的 skills 目录，能否加载该 skill 需要实际确认。

有内置 skill 安装器时优先使用它。安装仓库根目录时，源路径为 `.`，安装名称显式指定为 `antigravity-gemini-subagent`。没有安装器时，可将下载或克隆的完整源码文件夹放入目标目录，保留 `SKILL.md`、`agents`、`scripts` 和 `references` 等内容。

已有同名目录时，先检查是不是同一来源。内容相同则复用；更新时保留本地改动和可回退的副本，再同步已审查文件。不要删除整个 skills 目录或覆盖其他 skill。私有仓库使用已有 GitHub 访问权限，无法访问时明确报告，不改变仓库可见性。

## 3. 补齐缺失依赖

用户同时要求安装缺失软件时，按上表链接的官方或上游流程处理，保留当前主机要求的预检、备份和权限控制。先确认下载来源和安装步骤，再执行；不要顺手升级或重装已经可用的软件。

只要求安装 skill 时，可以完成源码安装及诊断，报告缺哪些依赖及对应安装来源。安装依赖的授权不包含登录新账号、导入其他人的凭证或新增账号权限；登录、OAuth 授权及用户未授权的账号导入由用户完成。账号 token、Cookie 和备份留在本机，不发送到模型或 GitHub。

内置模块会核对原生 CLI 的当前身份，需要至少一个匹配账号记录。用户登录由 CLI 完成，之后在有效预检下执行 `register-current`。只有显式选择 Tools 后端时，才检查其有效本地配置、管理认证和账号数据。不要编造配置或凭证让检查通过；模型调用始终使用原生 CLI。

## 4. 检查是否能使用

在安装后的 skill 目录中，用工作区内独立的审计目录运行：

```powershell
python -B scripts/package_skill.py
python -B scripts/workflow.py doctor --audit-dir <audit-dir>
```

`doctor` 检查本地环境，不启动 CLI 或管理工具，不刷新额度，不切换账号。它会写诊断记录。预检前仅因缺少预检记录而返回 `NOT_READY` 是正常的；CLI、账号或管理配置缺失则需要先补齐。

需要真实调用验收时，完成预检，再检查身份和发现模型：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/prepare.ps1
python -B scripts/workflow.py register-current --audit-dir <audit-dir> --preflight-dir <receipt>
python -B scripts/workflow.py doctor --audit-dir <audit-dir> --preflight-dir <receipt>
python -B scripts/workflow.py identify --audit-dir <audit-dir>
python -B scripts/workflow.py models --workspace <job-dir> --audit-dir <audit-dir> --preflight-dir <receipt>
```

用发现的实际 Gemini 模型执行一次无工具的简单任务并核对结果。该调用会使用模型额度，须在用户授权的安装验收范围内。默认安装验收不轮换全部账号，也不执行全面实测；换号测试仅在明确要求时进行。

已有匹配的兼容账号记录时可跳过登记。缺少 Python 或 CLI 时，按上表补齐；没有账号记录时，`doctor` 会给出 `ACCOUNT_STORE_EMPTY` 并提示登记。默认流程无需 Tools 管理服务。Tools 后端缺配置、未启动或认证失败时，按该后端诊断处理，不关闭用户已有会话。

## 5. 交付结果

分别报告以下状态，避免把“文件已复制”当成“能正常调用”：

- **已安装**：目标目录完整，语言文档和 skill 入口可读，来源提交已记录。Codex 可在安装后的下一轮使用 skill。
- **环境已就绪**：依赖、账号身份和有效预检通过检查。
- **真实调用已通过**：明确模型和思考强度下的一次简单任务返回且验收通过。

有缺项就具体说明缺的软件、配置、登录或权限，以及已完成的步骤。不要声称换号、指纹写入或真实额度恢复已验证，除非本次实际执行并取得相应证据。

分发时保留 [LICENSE](../LICENSE) 与 [NOTICE.md](../NOTICE.md)。本项目及移植部分采用 CC BY-NC-SA 4.0，包含非商业限制。
