# 原生 socket goal 交接实测（PAM-007c）

2026-10-10 在 VPS 进行私有、独立 specialist 生命周期试验。它不是
正式 Mesh task/租约/Leader 的验收；生产核心没有加载新的控制器，也
没有开启长期 goal。原生 Shell、文件、联网和 MCP 没有被 Mesh 拦截。

官方 Codex 0.159.2 实际 binary SHA-256：
`1748767b230ebfc3d4ab7e4e254920d0c0ad9691fd8c11f190e7d44511a4a92e`。
使用专用私有 Unix WebSocket listener 和数据目录，不复制正式历史或
配置。复用已有订阅认证，不创建 key/费用；旧 secondary 副本 401 后，
核对本人已有更新副本为同一 subject/email/account，仅用于隔离目录。
正式 profile 未覆盖。私有试验关闭自身记忆生成，不更改正式原生记忆。
传输 helper 的 6 项本地 socket fixtures 在 laptop 和 VPS Python 3.6
均通过；它是试验客户端，不是已发布的生产 transport。

## 阶段一：实际通过的边界

trial `PAM-007c-private-socket-drain-20261010-v1`，测试 thread
`01a12439-0b17-7911-a944-72d8e56a68b9`，turn
`01a12439-0c1e-7bc1-be3c-fd3970cea61f`。

模型实际调用专用动态测试工具，在原 RPC request 上等待。客户端在
同一进行中的 turn 设置测试 goal active，保留原历史字节；记录 intent
后只向运行器 PID 发一次 SIGTERM，不发进程组信号、不取消订阅、不
interrupt/pause。日志明确进入 graceful drain，runningAssistantTurns=1；
此后仅回复已捕获工具 request 一次，继续收原连接事件。

实际 1 个 admitted turn 自然 completed，运行器退出码 0，未强杀。
新 observer 只用 goal/get、thread/read、loaded/list；原 thread 未加载，
同 goal 仍 active，usage 从 0 增到 275，原生历史完成事件已落盘，
观察过的原字节前缀未改。最终 47191 字节原生历史 SHA-256：
`af40d36c02d167cd333dabe09d08297081fe278a662a85c23dd66a460a8ee34e`。

这证明一个已挂起工具的同线程原生 turn 可以被官方 socket admission
drain 后自然完成，并保留 active goal。它不证明一般工具、多个 children、
跨节点 lease fencing、运行器崩溃或正式 Worker 的安全 yielded outcome。

## 阶段二：未通过，保留 unknown

只对同一测试 thread 调用一次 resume，没有 goal/set 或 turn/start。
原生确实自动开始新 turn；首个为
`01a1243a-fb4e-74e3-9ed9-a8c3c85c8d81`。模型返回旧阶段的标记并表示
不再次调用 probe，没有进入预期的新阶段握手。试验不把 developer
instructions 的设置当成已经实际交付新任务输入的证明。

失败清理关闭连接后等待，随后强杀了这一个专用运行器 PID；这不是
安全 drain，也不是零效果。只读 observer 的实际历史已有 6 个 started、
5 个 completed，末轮
`01a1243b-29f4-7f22-a571-6246ed9507b6` 未见完成。goal 仍 active、用量
7671；没有为检查调用 resume/start。119293 字节历史 SHA-256：
`07547d1e26c84be839d045f2843aec2e29eb62d2a5f2344a4e6ad52d4a6bc4ff`。

阶段二结果保持 unknown_no_replay、原 ID、原目标和原历史；不新建
线程、重设用量、暂停目标或重新执行握手来制造通过结果。阶段一通过
不覆盖这份失败；阶段二也不倒改阶段一已核验的原结果。

## 下一片实现

1. 新 task/子任务结果输入必须显式、仅一次地送入实际自动 turn；
   持久化 intent，按当前 turn ID steer/adopt，不能假定 resume 配置
   就是模型收到新工作。超时或不明回执保留原身份，不能重试副作用。
2. 专用 socket 运行器失败退出时，先尝试一次 admission drain 并继续
   收所有 admitted turn 事件；连接关闭、超时或强杀仍 unknown。
   unsubscribe、stdio killpg、fork 新 Leader 均不等价于安全让出。
3. 在 Worker 增加独立 yielded outcome：只有排空、落盘、回收与原
   goal/history 核对通过，才可以等待 children；保留 active goal，
   不能借用要求 nonactive goal 的 terminal seal 或伪造暂停。
4. 持续原生历史 checkpoint、长 RPC 租约 watchdog 和真实 Mesh parent/
   child roundtrip 单独推进。Linux socket 是实测机制，其他环境由节点
   自适配相同合同，不变成通用原生能力限制。

官方依据：[socket 命令](https://learn.chatgpt.com/docs/developer-commands#codex-app-server)，
[graceful drain](https://github.com/openai/codex/blob/ff6aec96948b70d94983af2641a6b67c94faeff5/codex-rs/app-server/src/lib.rs#L248)，
[stdio 排除](https://github.com/openai/codex/blob/ff6aec96948b70d94983af2641a6b67c94faeff5/codex-rs/app-server/src/lib.rs#L775)，
[不加载线程的 goal/read](https://github.com/openai/codex/blob/ff6aec96948b70d94983af2641a6b67c94faeff5/codex-rs/app-server/src/request_processors/thread_goal_processor.rs#L247)。
原认证、RPC 全事件、rollout 和运行器日志留在私有目录，不进入仓库。
