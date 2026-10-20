# AgentPhone 0.4.4 原生接入

正常使用桌面程序时，默认启动会为本机已安装的 Codex、DeepSeek Harness、Claude Code 配置原生全局说明与四工具 MCP 接口，不需要普通用户手工导入这里的示例。
接口改为本机 stdio 代理，按 bridge-endpoint.json 跟随当前桥地址；不要继续使用旧网页演示的 8765 地址。
工具只有 get_agents、send_message、read_messages、get_conversation。发送与读取必须绑定原生真实会话，不可用随机会话 ID 或另起替身发起端。
发送工具返回的 inbox 为真实回信；awaiting_reply=true 时在同一原生上下文继续收信。

此目录的 cordis 文件仅为开发者示例，command 必须替换为实际 EXE 路径。旧 deepseek-reply.ps1 属于废弃网页演示，不用于当前版本，也不应在当前通信流程调用。
本版 DSH 目标必须使用原桌面服务创建/复用原生会话。headless CLI 不能冒充原桌面对话。
