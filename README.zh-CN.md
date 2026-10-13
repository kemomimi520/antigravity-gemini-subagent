# Antigravity Gemini Subagent

[English](README.md) | 简体中文

**让 Codex 把一部分任务交给 Gemini，并在同一段对话中继续协作。**

这个 skill 将原生 Antigravity CLI 接入 Codex 的工作流程。你可以让 Gemini 提供第二份代码审查意见、分析一个问题，或在指定项目中完成文件任务，再由 Codex 检查结果、整合到当前工作中。

如果任务中途遇到账号额度耗尽，Codex 可以通过项目内置的账号模块切换到另一个已有账号，验证切换结果后，接回原来的 Gemini 对话继续处理。

## 功能

| 功能 | 使用方式 |
| --- | --- |
| 委派任务 | 为 Gemini 指定任务、上下文、工作目录和验收要求 |
| 选择模型与思考强度 | 从 CLI 当前可用模型中选择，支持 `low`、`medium`、`high` |
| 文件与结构化输出 | 在指定目录中处理文件，或按 JSON schema 返回结果 |
| 多轮协作 | 保留对话 ID，追问、补充条件或继续未完成的任务 |
| 换号续接 | 确认账号额度耗尽后，通过原生换号流程继续同一段对话 |

适合把一个大任务拆出明确的子任务，也适合为重要结论增加一次独立检查。Gemini 收到的是 Codex 为它准备的任务上下文；结果返回后，由 Codex 负责验收。

## 快速开始

### 1. 准备环境

目前完整流程支持 **Windows**。需要安装 Python 和原生 [Antigravity CLI](https://antigravity.google/docs/cli/getting-started/)，并已登录可使用目标模型的账号。账号管理所需逻辑已集成在项目中，默认无需安装 Antigravity Tools。

运行时仅使用 Python 标准库，已测试 Python 3.11。模型通过 CLI 动态发现，不绑定某个每日更新的版本号。

### 2. 安装 skill

**方式一：把仓库交给模型安装**

如果你使用的模型能联网并操作本地文件，可以直接把下面这段话发给它：

```text
请安装这个 skill，并补齐缺失的软件依赖：
https://github.com/kemomimi520/antigravity-gemini-subagent
先阅读仓库的 references/installation.zh-CN.md，按该页完成安装、依赖检查和验收。
账号登录需要我操作时再提示我；已有软件和账号请保留。
```

[安装规范](references/installation.zh-CN.md)包含安装位置、缺失依赖的处理和验收标准。模型需要具备相应的本地操作权限；仓库私有时，还需要已有的 GitHub 访问权限。

**方式二：手动安装**

下载或克隆本仓库，将完整文件夹放入 Codex 的 skills 目录，文件夹名称保留为 `antigravity-gemini-subagent`。Windows 默认位置是：

```text
%USERPROFILE%\.codex\skills\antigravity-gemini-subagent
```

首次使用需要完成本地预检和备份，涉及管理员权限及系统还原点。自定义安装路径、账号管理目录和预检步骤见 [环境配置](references/environment.md)。

### 3. 在 Codex 中调用

例如，让 Gemini 帮忙审查代码：

```text
使用 $antigravity-gemini-subagent 审查当前项目的 src/parser.py。
从当前可用的 Gemini 模型中选择，使用 high 思考强度。
只读取文件，重点找边界条件问题，给出具体输入和修复建议。
完成后核实它的发现，再汇总给我。
```

后续可以接着说：

```text
继续刚才的 Gemini 子代理对话，让它针对已确认的问题提出测试用例。
```

Codex 会负责组织任务、调用 CLI、保存对话 ID 和检查结果。要使用固定模型，也可以直接在请求中指定当前可用的模型名称。

<details>
<summary>直接调用脚本</summary>

在 skill 目录中运行。尖括号内容需替换为实际路径：

```powershell
python scripts/workflow.py doctor --audit-dir <audit-dir>
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/prepare.ps1
python scripts/workflow.py models --workspace <job-dir> --audit-dir <audit-dir> --preflight-dir <receipt>
```

`receipt` 是预检成功后生成的目录。预检前，`doctor` 可能因缺少该记录返回 `NOT_READY`。

将下面的任务保存为 UTF-8 的 `task.json`，并把 `model` 替换为模型发现结果中的实际名称：

```json
{
  "task_id": "copy-explanation",
  "model": "gemini-REPLACE_WITH_DISCOVERED_MODEL",
  "effort": "medium",
  "prompt": "解释 Python 浅拷贝与深拷贝的区别，给出一个嵌套列表的例子，不使用工具。",
  "acceptance": "解释准确；例子包含修改嵌套元素后的结果；没有工具调用。",
  "read_only": true,
  "retry_safe": true
}
```

```powershell
python scripts/workflow.py run --task <task.json> --workspace <job-dir> --audit-dir <audit-dir> --preflight-dir <receipt>
```

更多参数、JSON schema 和结果字段见 [任务协议](references/protocol.md)。

</details>

## 还需要安装 Antigravity Tools 吗？

**默认不需要。** 本项目已将 Antigravity Tools 的相关账号逻辑移植为 Python 模块，提供本地账号登记、OAuth 刷新、额度查询，以及 Windows 凭证、CLI 文件凭证和已有设备配置的同步。

如果电脑上没有 Tools，在 CLI 登录后运行一次 `register-current`，即可登记当前账号。已有 Tools 账号数据也可以直接复用；默认账号目录沿用 `.antigravity_tools`，但内置后端不需要其 GUI 配置、可执行文件或本地管理服务。操作步骤见[安装规范](references/installation.zh-CN.md)。

原来的桌面管理方式保留为可选后端：`--account-backend tools`。管理工具的独立代理池绑定等未移植功能需使用该后端；内置后端会明确拒绝不支持的账号网络绑定，不会悄悄改走其他网络路线。详见[内置账号模块](references/native-accounts.md)。

## 工作方式

模型请求由原生 `agy` 发送给 Gemini；账号操作默认由内置模块完成。**本项目不通过推理反代接口调用模型。** CLI 和账号请求可沿用已有的 HTTP 网络代理。

内置账号模块按 Antigravity Tools 的凭证格式、设备字段和切换流程移植，再回读验证实际身份和适用存储。对话通过明确的 `conversation_id` 续接。若上一次调用已经修改文件，Codex 会先检查完成了哪些操作，再继续，避免重复执行。

额度恢复由 Codex 协调：确认额度耗尽 → 刷新已有账号额度 → 原生换号并验证 → 接回原对话。普通网络错误、服务过载和认证失败不会触发换号。详见 [账号切换](references/account-switch.md) 和 [对话续接](references/conversation.md)。

## 当前验证范围

Windows 实测已覆盖 Gemini 调用、模型与思考强度选择、结构化输出、文件任务，以及原生换号后的跨账号续聊。内置模块另已验证全新账号目录登记、8 个账号的直接额度刷新，以及不调用 Tools 服务的换号续聊；测试后原始凭证和文件内容已恢复。GitHub CI 运行合成状态下的离线回归和源码检查，不需要真实账号。

仍需留意：真实额度耗尽触发的完整恢复链路尚未实测；旧版指纹存储写入分支只有合成测试和源码审查覆盖。模型校验确认的是 CLI 选择的名称，不是服务端内部模型代号。完整记录见 [测试说明](references/test-workflow.md)。

原生 CLI 与 Antigravity 2.0 共享 Windows 账号凭证，换号时需要协调其他会话。只读模式使用 CLI 的 plan 模式，不提供操作系统级隔离。任务输出仍需验收；日志和备份应保留在本地，可能含有敏感内容。

## 文档与开发

- [Skill 指令](SKILL.md)：Codex 如何使用这个子代理。
- [安装规范](references/installation.zh-CN.md)：交给模型安装、补齐依赖与验收。
- [环境配置](references/environment.md)：依赖、路径覆盖、网络和预检。
- [任务协议](references/protocol.md)：输入、结果、工具权限和失败状态。
- [账号切换](references/account-switch.md)：凭证、设备配置、验证与恢复。
- [内置账号模块](references/native-accounts.md)：无需桌面工具的账号登记与后端选择。
- [对话续接](references/conversation.md)：多轮任务与文件检查点。
- [测试说明](references/test-workflow.md)：离线回归与真实调用。

运行离线检查：

```powershell
python -B -m unittest discover -s tests -v
python -B scripts/package_skill.py
```

提交内容限于源码、文档和合成测试；账号凭证、任务日志、对话记录和回滚备份不应进入仓库。源码检查器覆盖文件白名单和常见秘密模式。

## 许可证

本项目采用 [CC BY-NC-SA 4.0](LICENSE)：署名、非商业、相同方式共享。内置账号模块源自 [Antigravity Tools](https://github.com/lbjlaq/Antigravity-Manager)，上游来源和修改说明见 [NOTICE.md](NOTICE.md)。商业使用需取得相关权利人的额外授权。
