# Adapter 开发说明 · 0.4.7

桥只传话。新增 Adapter 必须保持真实 NativeSID、实际收件回执和显式发送协议；不生成规划、总结或替身发起 Agent。

每个目标还必须接入其原桌面对话，不能以终端、桥内消息栏或新空聊天代替。
SessionAdapter.context 初始化 NativePresentation，按本连接独立 runtime 目录保存首次开窗记录。
目标接受首条消息后调用 present_conversation()；open_native_conversation() 只打开当前真实 native_session_id。
公共 show_once 仅在打开成功后记录连接和原生 ID；重复调用、适配器重建都不会重新弹窗，新连接独立计数。
若对话接口未接好，desktop_conversation_available 返回 False，get_agents 标记 can_contact=False，连接请求被拒绝。
本版启用 DSH、Codex 和 WorkBuddy；Claude/ACP 通信代码保留作后续开发基础，但缺少已验证原桌面接口，目标禁用。

DSH 使用原桌面服务会话和已有窗口选择接口；Codex 必须使用持久化原生 thread 与官方指定会话的 deep link。
运行时和桥都不能自动批准原 Agent 的权限请求，不得修改用户原账户/模型配置。

验证：首次开窗一次、第二轮不抢焦点、相同 NativeSID、重建 Adapter 不重复、开窗失败不记成功、新连接可再开、禁用目标不会破坏有效旧连接。提交真实原软件调用证据；协议 fixture 不能冒充桌面对话或模型实测。


DSH 工作区指令通过原生 AGENTS.md 加载，不混入可见正文。四工具接口默认回收真实回信；B 发信不等待。常驻 stdio 代理必须传真实 sender_session_id，不猜共享进程身份。仅原生 EXE 从其当前 shell 继承 Codex 真实 ID。原生全局配置幂等、备份、保留其他设置，不能改审批或模型配置。


已启用桌面适配器不得设置整轮工作完成时限。传输接口的响应期限不等于工作期限。
DSH 在真实 MCP 收信回执产生后可返回 wait 完成投递，释放后续消息队列；不要求原生 turn/end。
没有实际收信回执时仍必须验证显式通信协议。只有明确 send_message 会产生回信。
DSH 只读查询可恢复重试；提交 prompt 超时不得盲目重发。连续读取故障只停止观察并报告，不取消原 Agent。
DSH stop_session 只设置桥停止标记，不能调用 session/cancel。原程序不是桥的子进程，不可终止其工作。
测试要覆盖工作超过旧时限、收信后继续工作与发信、后续队列、读取故障与桥错误清理；不能把模拟回归写成模型实测。


本版原生选择与续接：NativeSessions 仅列元数据、检查身份、按用户点击创建空 A 对话；不能调用 turn/start 或 session/prompt 来握手。Codex 空 A 使用原生 thread/inject_items 保存通信身份元数据，否则 start-only 线程没有可恢复 rollout；该 API 不发起模型调用。查询连接与真正投递连接分开。
DSH list 的参数名为 _request；其余会话接口为 request。续接必须采用 session/list 返回的原 cwd，否则原 Host 拒绝身份/路径冲突。已有对话不重命名，不写其项目指令。
Codex 续接仅 thread/resume(threadId=原 ID)，保留原配置，不 fork/start。DSH 原窗口选择前核对原 ID、标题和唯一性，助手只操作原应用 PID；同名不能假装选对。
UI 断开是可续接的 DISCONNECTED；新连接释放旧路由。旧 FAILED 投递记录不改成功、不重播；连接错误是当前传输状态，可以在真实端点恢复后清除。原 A 或真实 B 绑定均可继续原 CID，禁止另一个原生对话借身份。
选择器配置保存在用户数据目录 bridge-selection.json，仅传输配置；不进入发布包。四工具保持 get_agents、send_message、read_messages、get_conversation。


WorkBuddy 适配：workbuddy_desktop.py 客户端只连接由所选 WorkBuddy.exe 宿主加载的 agentphone-bridge 扩展。健康检查核对 protocol/PID/父进程/应用路径，原桌面协议必须指向同一程序。扩展为 platform，显式声明 conversations.local.list/get/create/rename/runPrompt，禁止使用 builtin、* 权限或任意 RPC 转发。HTTP 只绑定 127.0.0.1，认证使用扩展自己的随机本机 token，拒绝 Origin、错误 Host 和未认证调用。不得读取云模型密钥或接管另一个 ACP 客户端所有权。
原生接口使用 WorkBuddy 内部公开扩展 SDK wb.invoke，通过它的 conversations 集合创建、查询和 runPrompt；不是另起 CLI 模型进程。已有对话只 get 原 ID，不 rename/create。原生 runPrompt 不传 timeoutMs，桥断开不调用 cancel/destroy/resolvePending。只取最终公开文本块，禁止串接工具前 commentary 与最终协议。四工具和真实收信验证保持不变。原生 SessionStart hook 提供自己的实际 ID，MCP 配置位于 ~/.workbuddy/mcp.json；全局说明 ~/.workbuddy/CODEBUDDY.md；不覆盖其他 hooks、MCP、模型或审批设置。
扩展是随 AgentPhone 发布的本机代码，由 WorkBuddy 正常启动扫描 ~/.workbuddy/extensions/ 加载；不改 app.asar、不注入 JavaScript、不模拟桌面端、不绕过权限。初次安装时运行中的 WorkBuddy 需正常重开；不要强行终止用户任务。SDK 接口版本变化应报告兼容错误，不能自动创建替代会话。
