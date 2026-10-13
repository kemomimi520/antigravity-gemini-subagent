# Antigravity Gemini 子代理

[English](README.md) | 简体中文

通过原生 Antigravity CLI（`agy`）执行 Gemini 子代理任务的 Codex skill。支持明确指定模型和思考强度、按对话 ID 继续任务，以及通过 Antigravity Tools 原生切换账号后续聊。

主代理负责下发范围明确的任务并独立验收结果；Python 辅助脚本负责调用 CLI、校验协议和账号身份、记录审计信息，以及检查切换后的状态。子代理不会自动继承主代理的整段对话，需要主代理显式提供任务和必要上下文。

## 能做什么

- 使用当前 CLI 实际发现的 Gemini 模型，指定 `low`、`medium` 或 `high` 思考强度。
- 捕获流式输出，校验工具调用、权限、结构化结果和账号身份，生成脱敏后的审计记录。
- 使用明确的 `conversation_id` 继续同一段原生对话，包括切换账号后的续聊。
- 刷新已有账号额度，并仅在确认当前账号额度耗尽后执行受监督的换号恢复。
- 按任务范围临时调整文件访问设置；任务产生部分文件修改后，先检查检查点和真实差异，再决定是否继续。

**模型推理走原生 CLI，不调用账号管理工具的推理反代接口。** 现有 HTTP 代理可能承载 CLI 的网络流量，这与使用管理工具的推理反代是两回事。脚本不会修改系统代理，也不设置每日版本号白名单；兼容性依据实际能力和操作后的校验结果判断。

## 实现与账号切换

主代理读取 [SKILL.md](SKILL.md)，通过 [workflow.py](scripts/workflow.py) 调用原生 CLI。新任务使用显式模型；续聊使用原生 `--conversation` 参数，传入指定对话 ID，不猜测“最近一次对话”。

换号调用 Antigravity Tools 本机管理服务的原生接口 `POST /api/accounts/switch`，显式指定 `targetIde: "agy"`。管理请求限定在本机回环地址，不经过继承的代理，也不接受重定向。账号切换和模型推理是两条不同的调用路径。

凭证和设备配置由换号工具的原生逻辑处理：保留已有绑定设备配置，缺失时由管理工具绑定；随后写入共享的 Windows 凭证，同步适用的 CLI 凭证文件，并处理实际存在的指纹存储。辅助脚本对账号身份、完整凭证、管理工具选中账号、已绑定设备配置和适用指纹字段进行回读校验。它不会另建一套独立的换号凭证或设备指纹机制。

当主机没有旧版指纹文件或数据库时，结果记录为 `no_legacy_storage_present`，不能声称完成了实际指纹写入。失败后的恢复也需要检查是否仍拥有相关状态，避免覆盖其他会话的并发修改。详细流程见 [原生换号与回滚](references/account-switch.md)。

## 环境要求与安装

**完整流程目前支持并实测的是 Windows。** 不能依据上游工具支持其他平台，或纯 Python 离线测试通过，就宣称完整支持 Linux/macOS。

需要具备：

- Python。运行时和离线测试使用标准库，已测试 Python 3.11；不锁定具体补丁版本。
- 已安装、已登录且有权使用目标模型的原生 `agy`。
- 已安装 Antigravity Tools，并已有可用账号清单。
- 辅助脚本、CLI 和管理工具处于同一 Windows 用户的凭证环境。
- 可访问模型服务的网络，以及真实调用所需的预检与备份环境。

将完整仓库文件夹放入接收代理配置的 skills 目录，保留文件夹名 `antigravity-gemini-subagent`，以及 `SKILL.md`、`agents`、`scripts`、`references` 等内容。首次真实调用前阅读 [环境与路径说明](references/environment.md)。这些脚本不会替你安装软件、登录账号或开通模型权限。

`prepare.ps1` 优先使用主机现有的 managed preflight；仅当该文件不存在时，才使用随仓库提供的 `managed_preflight.ps1`。仍需管理员权限和已创建或近期的系统还原点。备用脚本不能绕过主机预检失败；复制 skill 也不代表获得提权或修改账号的授权。

在 skill 文件夹内检查本地前置条件：

```powershell
python scripts/workflow.py doctor --audit-dir <已授权的审计目录>
python scripts/workflow.py identify --audit-dir <已授权的审计目录>
```

这两条命令不会启动 CLI 或管理工具。未完成预检时，`doctor` 会因缺少有效凭据报告 `NOT_READY`，属于预检前的预期结果。

按当前主机授权完成预检：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/prepare.ps1
```

随后将实际成功的预检目录传给 `--preflight-dir`，也可用该参数重新运行 `doctor`。不能伪造预检记录或跳过还原点门槛。预检备份可能含完整账号凭证，必须保留在本地。

### 路径配置

命令参数优先于环境变量及默认发现路径。

| 用途 | 命令参数 | 环境变量 |
| --- | --- | --- |
| 管理工具数据目录 | `--manager-dir` | `AGY_SUBAGENT_MANAGER_DIR` |
| 管理工具可执行文件 | `--manager-cli` | `AGY_SUBAGENT_MANAGER_CLI` |
| CLI 可执行文件（`models/run/doctor`） | `--cli` | `AGY_SUBAGENT_CLI` |
| 文件权限配置（`run/doctor`） | `--settings-file` | `AGY_SUBAGENT_SETTINGS` |

`--settings-file` 必须指向 CLI 实际加载的设置文件；它只决定辅助脚本临时修改哪个文件，不会重定向原生 CLI 的配置加载路径。自定义数据或设置路径时，应同时通过 `prepare.ps1` 的 `-ManagerDir`、`-SettingsFile` 参数将目标纳入备份。默认路径与发现逻辑见 [环境说明](references/environment.md)。

## 第一次调用

以下命令中的尖括号内容是占位符，执行前替换为实际路径或模型名称。

预检成功后，获取当前可用模型：

```powershell
python scripts/workflow.py models --workspace <已授权的任务目录> --audit-dir <已授权的审计目录> --preflight-dir <成功的预检目录>
```

将任务保存为 UTF-8 JSON 文件。下面是无工具算术任务的示例；`model` 要替换为模型发现结果中的实际 Gemini slug。

```json
{
  "task_id": "sum-example",
  "model": "gemini-<discovered-slug>",
  "effort": "medium",
  "prompt": "不使用任何工具，计算 17 + 25，返回指定的 JSON 对象。",
  "acceptance": "structured_output 等于 {\"sum\":42}；没有工具调用",
  "read_only": true,
  "retry_safe": true,
  "max_attempts": 3,
  "json_schema": {
    "type": "object",
    "properties": {"sum": {"type": "integer"}},
    "required": ["sum"],
    "additionalProperties": false
  }
}
```

运行任务：

```powershell
python scripts/workflow.py run --task <task.json> --workspace <已授权的任务目录> --audit-dir <已授权的审计目录> --preflight-dir <成功的预检目录>
```

主代理应独立确认 `structured_output` 等于 `{"sum":42}`。`acceptance` 文本用于提示子代理，辅助脚本不能自动判定任意自然语言验收标准；退出码为 0 或 JSON schema 通过，都不能替代正确性验收。

继续对话时，使用新的 `task_id`，并在任务中填入前一次返回的 `conversation_id`。需要更多协议、工具和文件检查点说明时，阅读 [任务协议](references/protocol.md) 和 [对话续接](references/conversation.md)。

## 额度不足后如何继续

仅当确认结果为 `QUOTA_EXHAUSTED`，主代理才按以下流程恢复：

1. 刷新已有账号额度，按同一模型选择有新鲜额度信息的候选账号。
2. 通过 Antigravity Tools 执行原生 `agy` 换号，并验证凭证、身份、选中账号及适用设备配置。
3. 检查原任务是否产生部分修改，确认检查点及重试条件。
4. 使用原 `conversation_id` 进入同一段子代理对话，继续任务并验收结果。

这是主代理监督下的多步流程，不是后台自动轮换器。`max_attempts` 默认 3，可显式设置更大的正整数有限预算；失败账号不会重复尝试。普通 429、泛化的 `RESOURCE_EXHAUSTED`、服务过载、模型资格或认证错误、网络错误和超时，不足以授权换号。

原生 CLI 与 Antigravity 2.0 共享 Windows 凭证。单次操作锁不能阻止外部应用或其他会话改写身份，也不覆盖整段多命令恢复流程；应协调换号与续聊全过程。未知的切换结果会保留证据供检查，不会盲目重试。流程不使用 IDE 界面点击，不创建账号、购买额度或自动降级模型。

`read_only` 使用 CLI 的 plan 模式，**不是操作系统级沙箱**。原生全局规则和 MCP 配置可能加载；只向子代理提供已授权的数据，续接部分任务前检查实际文件差异。

## 测试范围与已知限制

运行离线回归：

```powershell
python -B -m unittest discover -s tests -v
```

离线测试使用合成状态，不需要真实账号凭证；GitHub CI 仅运行离线回归和源码检查。真实调用脚本需要对应授权、有效预检和模型额度。具体流程见 [测试说明](references/test-workflow.md)。

已有 Windows 实测覆盖子代理返回结果、原生账号切换、按明确对话 ID 跨账号续聊，以及带检查点的文件任务。仍有以下边界：

- 实测主机没有旧版指纹存储或数据库，对应写入分支只有合成测试与源码审查覆盖，没有真实写入验收。
- 尚未遇到真实账号额度耗尽并触发完整恢复链路；不能把主动换号续聊实测等同于该触发链路全部实测，也不应人为耗尽额度制造条件。
- `model_verified` 仅表示 CLI 初始化返回的模型 slug 与请求一致，不表示已独立核实服务端实际模型代号。此前诊断探测不是可移植的正式功能；模型后缀和知识问答不能证明官方模型能力。
- 多个子代理不能据此获得彼此隔离的账号凭证；共享身份和外部会话并发仍需协调。

## 源码打包与隐私

只提交源码、文档和合成测试。真实任务、输出、日志、账号清单、对话存储、凭证和预检回滚包应放在源码目录之外。`.gitignore` 只能减少误提交，不会清除已有秘密或撤销提交。

检查源码及常见秘密模式：

```powershell
python scripts/package_skill.py --root <skill目录>
```

添加 `--output <源码目录之外的新ZIP路径>` 可创建仅含源码的压缩包；已有包不会被覆盖。检查器不会读取私有账号目录，也不能保证识别全部秘密。

脱敏覆盖已知 token 和邮箱模式，不能保证移除任意秘密、私有路径或任务数据。原生日志在后处理前可能含敏感内容，进程突然终止可能留下未脱敏记录。备份会有意保留完整状态，包括凭证，禁止上传。若秘密已经提交到 GitHub，应撤销或轮换凭证，并参考 [GitHub 敏感数据移除说明](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)。

辅助脚本替换私有文件前，会复制并验证现有 Windows DACL 和继承保护状态，失败即停止写入。这不等于保留全部文件属性、所有者或 SACL，也不能约束原生 CLI 和管理工具内部的写入行为。

仓库尚未选择许可证。公开发布前应选择并添加许可证；此介绍页不授予许可证权利。

## 进一步阅读

- [环境要求](references/environment.md)：平台、路径、网络与预检。
- [任务协议](references/protocol.md)：参数、结果和失败分类。
- [账号切换](references/account-switch.md)：原生凭证、设备配置、校验与回滚。
- [对话续接](references/conversation.md)：明确对话 ID、文件检查点和部分执行。
- [测试流程](references/test-workflow.md)：离线与真实调用的范围。
- 上游实现：[CLI headless 文档](https://antigravity.google/docs/cli/headless/)、[管理工具原生集成源码](https://github.com/lbjlaq/Antigravity-Manager/blob/main/src-tauri/src/modules/integration.rs)。
