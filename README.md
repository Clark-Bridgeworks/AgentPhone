# AgentPhone

**让两个真实 Agent，在各自的原生对话中互相传话。**

AgentPhone 是面向 Windows 的本机 Agent 通信桥。它连接两边已有或新建的原生会话，转发 Agent 显式发送的消息，并展示实际投递状态。用户继续在原来的 Agent 软件中提出需求、查看结果和处理审批。

> A local communication bridge for independent agents. Keep native sessions, exchange explicit messages, and inspect delivery receipts without moving your work into another chat application.

## 功能

- **原生会话连接**：选择 A/B 两端的 Agent，以及已有或新建的原生对话。
- **显式双向通信**：发送、接收、回复关联与真实回执；普通最终输出不会自动变成发信。
- **断开与续接**：保留连接和原生会话标识；失败消息不会自动重放。
- **本地持久化**：SQLite 保存桥内消息及投递状态，重启后可继续读取未读回信。
- **四个 MCP 工具**：`get_agents`、`send_message`、`read_messages`、`get_conversation`。
- **桌面界面**：深色主题、Agent 检测卡片、A/B 消息面板、连接选择器和状态提示。
- **可扩展适配器**：按目标软件的真实接口接入，不启动替身 Agent 冒充原对话。

本仓库发布 0.4.7 的源码及 2026-10-20 的深色界面更新。Python 服务内部的协议版本字段可能仍显示为 0.1.0。

## 当前接入范围

| Agent | 接入方式 | 状态 |
| --- | --- | --- |
| Codex | 原生 app-server 会话与桌面对话链接 | 已实现，需要相容的官方 CLI 和桌面协议 |
| DeepSeek Harness | 原桌面服务与原生会话 | 已实现，依赖目标软件提供的本机接口 |
| 腾讯 WorkBuddy | 原生扩展宿主中的通信扩展 | 已实现，接口可用性以运行时检查为准 |
| Claude Code / Gemini CLI / Qwen Code / Kimi CLI | 保留部分协议适配代码 | 缺少已验证的原桌面对话接入，目前不能作为连接目标 |
| Ollama | 本机安装检测 | 不是本版可联系的目标 Agent |

发现了某款软件，并不表示它能够接入。账户、登录、模型额度及操作审批仍由各原软件管理；第三方软件更新可能影响适配。

## 从源码运行

要求 **Windows、Python 3.11+ 和 Tkinter**。官方 Windows Python 安装程序通常包含 Tcl/Tk。原生 Agent 桌面集成以 Windows 为主要支持环境。

在项目目录中运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[desktop,dev]"
.\.venv\Scripts\python.exe -m agentphone.desktop
```

在界面中重新检测 Agent，点击“选择两边对话”，选择两端软件和对话。随后回到所选 A 的原生聊天中提出需求，让它使用 AgentPhone 联系另一端。

默认数据位于 `%LOCALAPPDATA%/AgentPhone/ABBridge`。开发时可使用独立目录：

```powershell
.\.venv\Scripts\python.exe -m agentphone.desktop --data-dir .\local-data
```

独立目录中的桥、MCP 代理和工具调用必须使用同一个 `--data-dir`。不要把两个不同的数据目录混用。

### MCP 接入示例

源码运行模式可以在 Agent 的 MCP 设置中使用 Python 入口，路径请替换为本机实际安装位置：

```json
{
  "mcpServers": {
    "agentphone": {
      "command": "C:/path/to/AgentPhone/.venv/Scripts/python.exe",
      "args": ["-m", "agentphone.desktop", "--mcp-stdio"]
    }
  }
}
```

各厂商的配置格式不同，上面只是通用结构示例。源码启动不会自动写入 Agent 的全局配置；打包后的 EXE 在默认数据目录运行时，会为检测到的支持软件安装相应的桥接说明、MCP 入口或会话身份钩子，修改前会备份相关文件。首次安装 WorkBuddy 扩展后，需要正常退出并重新打开 WorkBuddy。

### 原生会话身份

发送和读取消息必须显式提供真实的 `sender_session_id`：Codex 可在当前原生 shell 中读取 `CODEX_THREAD_ID`，WorkBuddy 使用其原生 SessionStart 身份或 `CODEBUDDY_SESSION_ID`。其他 Agent 应从自身运行环境获取实际身份，不能借用选择列表中别人的 ID。

`send_message` 返回的 `inbox` 是真实回信。若返回 `awaiting_reply=true`，应在同一连接下继续调用 `read_messages`，并携带上次读取到的 `after_message_id`。切换目标时使用新连接，不能把旧连接身份移植给另一个原生聊天。

## 开发与测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q --ignore=tests/test_desktop_layout.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_desktop_layout.py::test_footer_and_dual_panes_fit_minimum_window_with_all_detection_cards
.\.venv\Scripts\python.exe -m pytest -q tests/test_desktop_layout.py::test_wrapped_message_card_preserves_selectable_readonly_chinese_content
.\.venv\Scripts\python.exe -m pytest -q tests/test_desktop_layout.py::test_selector_offers_existing_or_new_on_both_sides_without_sending_tasks
node tests/workbuddy_extension.cjs
```

Windows 原生 Tk 界面测试分别放在独立进程中运行，避免窗口对象在后台线程中回收。测试使用临时数据目录和受控协议端点。通过这些测试不等同于所有厂商、所有版本均完成真实模型对话验证。适配器开发细节见 [Adapter开发说明.md](Adapter开发说明.md)。

### 构建 Windows EXE

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --distpath dist --workpath build scripts/AgentPhone.spec
```

输出为 `dist/AgentPhone.exe`。仓库同时提供 `dsh_window.cs` 及运行时需要的窗口辅助程序；如需重新编译，可在 Windows PowerShell 中使用：

```powershell
& "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /target:winexe /out:src\agentphone\desktop\native\dsh-window.exe src\agentphone\desktop\native\dsh_window.cs
```

构建前应在当前虚拟环境中安装本项目，以便打包工具收集模块和资源。项目代码采用 MIT 协议；Python 及第三方依赖保留各自的许可证。

## 项目结构

```text
src/agentphone/          HTTP API、消息存储与桥接服务
src/agentphone/desktop/  桌面界面、发现、原生会话与适配器
scripts/                桌面打包入口和独立服务示例
connectors/             接入示例
tests/                  规则、持久化、适配器与界面测试
```

## 数据与产品边界

桥内只显示 Agent 显式发出的通信，不转发隐藏思考，也不会自动转发整个原生对话。原 Agent 的操作继续遵守用户已有的权限及审批设置。

本项目只负责连接、传话和展示收发状态，不提供额外的规划器、任务分工、群聊、代审批或自动总结。`PENDING`、`DELIVERED`、`PROCESSED` 和 `FAILED` 描述投递处理状态，不能当作对方已完成工作的证明。

公开源码不包含用户数据库、对话记录、登录凭据、模型密钥、本机配置及配置备份。仅在可信的本机环境运行桥服务，默认保留回环地址绑定。

## 参与贡献

欢迎提交问题和改进。新增适配器请说明目标软件版本、原生会话身份来源、真实收发回执及桌面对话打开方式，并覆盖续接、失败处理和重复投递的回归测试。模拟协议测试和真实软件验证应分别标注。

## 许可证

[MIT License](LICENSE) · Copyright © 2026 Clark-Bridgeworks
