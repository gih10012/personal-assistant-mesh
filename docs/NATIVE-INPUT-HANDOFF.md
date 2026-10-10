# 原生 goal 交接前置合同（PAM-007c）

2026-10-10 源码候选。它补齐输入回执、可选传输、scope 归属、opt-in drain
及独立 goal/history seal，**没有启用正式 active goal yield**。运行期仍为冻结 `3198c87`。既有私有
resume 失败的原 thread、active goal、历史与 unknown 均保留，不重放。

## 新输入不是 resume 配置

官方 `turn/steer` 把输入追加到进行中的 turn，必须提供实际
`expectedTurnId`，返回 `{turnId}`；不会产生新 turn/started。
[官方接口](https://learn.chatgpt.com/docs/app-server#steer-an-active-turn)

同一 task 的新工作或 children 结果按以下边界交付：

1. 保留原 thread，先持久化 resume 的效果 guard，再读取实际 goal。
2. resume/RPC 期间暂存服务端请求，暂不执行 Mesh host 回调。只有
   当前 turn 的真实 started 通知才能确定输入目标，不能从文字猜测。
3. 写入一次性输入 intent：thread、turn、方法、输入 SHA-256。持久化
   成功后才提交一次 steer；原文不写入诊断记录。
4. 只有实际响应的 turnId 精确匹配，才记录 submitted，再处理原
   thread/turn 的已暂存请求。回执缺失、错误或超时保持 unknown，
   不再提交、不重放请求，也不能终结效果 guard。

暂存只约束 Mesh 自己的 host 回调，并不是原生 Shell、文件、网络或
MCP 白名单。原生自动 turn 在 resume 时仍可能开始工作，因此这项
回执保护**不声称新输入在一切原生操作之前到达**；强实时租约与原生
进程整体排空仍需下一阶段验证。

已 yielded 的目标在目的节点缺失，或原 createdAt/用量不连续时，直接
保留未核验状态，不从配置重新 goal/set。原生目标和计费状态不等于
rollout 文件；只传历史不能声称目标已迁移。实际 paused/blocked 等
状态不会被新任务指令隐式重启。新 objective 会重设用量，故不能用来
模拟恢复。[官方 goal 状态](https://learn.chatgpt.com/docs/app-server#manage-a-thread-goal)

## 可选私有 Unix WebSocket

Codex 子配置的 `native_transport` 默认为 `stdio`；显式 `unix` 使用
本人的 0700 临时入口目录，原有 auth/memories/plan/权限
配置保持。没有 TCP 公网监听、第三方依赖、新 key 或新费用。缺少
AF_UNIX 或 Linux owned-peer 证明的环境明确不支持该选项，节点可以继续 stdio 或自行适配
相同生命周期合同；Linux 机制不是所有平台的限制。
[官方 listener](https://learn.chatgpt.com/docs/developer-commands#codex-app-server)

0.159.2/0.162 官方 listener 将入口发布为 symlink：物理 socket 位于
canonical `/tmp/codex-daemon-{euid}` 的 0700 目录，文件名为 canonical
入口路径 bytes 的 SHA-256 纯 hex（无 `.sock`），模式 0600。
[已核对的 listener 源码](https://github.com/openai/codex/blob/ff6aec96948b70d94983af2641a6b67c94faeff5/codex-rs/app-server-transport/src/transport/unix_socket.rs)、
[物理目录合同](https://github.com/openai/codex/blob/ff6aec96948b70d94983af2641a6b67c94faeff5/codex-rs/uds/src/daemon_directory.rs)。

仅 Codex adapter 接受自己随机创建且身份未变目录内的官方最终 alias：
readlink 必须精确匹配计算出的绝对物理路径，真实祖先不得增加链接链，
目录/socket 所有权和模式都需核验。直接连接物理路径，握手前以 Linux
SO_PEERCRED 验证精确 owned Popen PID/UID，连接前后复核路径身份；
不得按同 UID、进程组、名称或猜测子孙 PID 放行。通用 UnixWebSocket
仍拒绝 symlink。目录权限与快照不声称是对同 UID 恶意进程的原子沙箱。

Unix 模式的 `executable` 应指向官方原生二进制或真正 `exec` 它的
包装器。npm CLI 的 Node 启动器另 spawn native，PID 不同会被拒绝；
按本节点安装路径配置 native binary，不改全局命令或认证。版本升级
后若路径合同变化就拒绝连接，不自动寻找未知 daemon 或修其权限。
正常清理只删除身份/readlink 都未变的自身 alias 和身份未变的空入口
目录；绝不 chmod/unlink 共享 physical socket、目录、锁或别人的入口。

传输校验升级握手、帧、JSON 与上限，接收超时保留部分帧。写入失败
可能已被服务端接收，因此不会自动重试。关闭只向自有 app-server
PID 发一次 TERM；超时清理若必须 KILL，或者 reader 强制关闭/退出码
非零，不能通过 goal terminal seal。清理本身不证明协同 yield。

`native_goal_drain` 默认为 false。显式 true 才允许以下排空流程，且必须
已验证 owned Unix peer；stdio 不以 TERM 模拟成功。不加载 thread 的
独立 goal/history readback 尚未实现。默认 active goal 等子任务仍返回
`codex_goal_coordination_yield_unavailable`；opt-in 排空成功只记录
`drained_unverified`，Worker 返回 `codex_native_yield_seal_required`，
保留原身份和 guard，不能因本地退出成功就放行。

## 子任务与原 goal 的归属

本地 delegate 不复制父 goal/goal_mode；新建子 task 的 project、scope、
authority、机器 fences 与原生权限保持。Worker 也去掉 delegated child
的节点默认 goal，忽略旧本地 child 偶然继承的 context.goal。远端 A2A
已有明确 `/goal` task 合同仍保留。模型自行创建的原生目标工具未禁用。
本地 delegate 尚无显式 child goal 合同，不能静默复制父预算。

Store 为严格记录的 `outcome=yielded`、已回收/settled、同 thread 与
同 active goal 保留 scope：children 等待、wake-pending 和没有开始
原生工作的 auth/backend backoff 都不能让另一任务抢走 Leader。
不同 specialist scope 不受阻；最后一个 child 或提前完成的 children
仍唤醒**原父 task**，下一次 claim 使用新 epoch，不另建任务。

这些检查验证 Worker 提供的记录，而不是物理运行器证明。未来 Worker
必须先完成 admitted turns、回收、历史稳定性和原 goal 独立核验，才
可提交 yielded/false guard；当前没有代码生成这份成功 yield。

## 验收边界与下一步

`ee6f924` 同一冻结代码两端完整 1449 项及公共 CI 成功；本机 native
0.162/VPS native 0.159.2 的零模型实际检查均 initialize 成功、没有
loaded thread、自然退出码 0、reader 已回收，无强杀。未 start/resume
thread、未 set goal，不重放旧 unknown；正式服务保持旧冻结 `3198c87`。
原 `455430d` 虽两端 1424 项/CI 通过，其实机 alias 拒绝失败仍保留，
不会把 fixture 通过说成实机成功。

协议/状态 fixtures 和 idle 连接都不是模型已收到新输入的真实证明。
继续推进：正式接入 drain event pump 与独立 yielded seal；原 task
等待/唤醒/同 goal 输入的真实 roundtrip；连续原生历史 checkpoint 和
长 RPC lease watchdog。之后再加载正式控制器与 3–5 天前沿检查。
Chat 模型 tools/OAuth 与 Codex Cloud job/node 仍单列未接通。

### 排空切片与后续独立交接

`PAM-007c-2` 已实现为默认禁用的源码候选：`finish` 在当前 turn 内也检查 wait，先立本地 draining
gate，再强制 tick 持久原 task/epoch/runtime 的 TERM intent，成功 ACK
之后才一次 PID-only TERM。Codex RPC 与 Worker optional steer 双侧
停止本轮控制器主动追加工作；heartbeat/lease fence 和已 admitted host
响应仍继续。这不是限制任何节点原生功能的全局白名单。
信号与 admission 的适配依据已核对的
[官方 SIGTERM/drain 实现](https://github.com/openai/codex/blob/ff6aec96948b70d94983af2641a6b67c94faeff5/codex-rs/app-server/src/lib.rs#L200)。

独立 drain pump 先消费 deferred，再读 raw queue，每次短等待后 tick；
不能直接复用 `event()` 先执行 host callback 的路径。先验证 request
属于原 thread/实际 admitted turn，callback 前预留原 request ID，重复
或回复不明不能重调。允许观察 TERM 前 admission race 的迟到 started，
但每个实际 turn 必须有自然 `status=completed`，不能把 interrupted/
failed 也计入的 completed ID 集合当证明。无未答请求/未结 RPC、实际
idle、自然退出 0、wait/reap 与 reader EOF/join 缺一都保留 unknown；
不以 `close()` 的阻塞等待、断连接或强杀替代。

draining、已发 TERM 或 drained 的运行器不能用新的 start 清除 gate。
tick、host callback、join 或最终 activity 返回后若已超时，不返回排空
成功；原生 callback 仍可能阻塞，并不声称硬实时 deadline。当前 Worker
在 drained 后不再调用 native_rollout/goal RPC，保持 settled/quiescent
为 false。23 项新 drain fixture 和 6 项 Worker 边界 fixture 已通过；
原生近邻合计 238 项通过。新 adapter 尚未进行 active 模型实测，
不能借用此前独立私有 probe 作为本版本执行证明，也未启用正式配置。

`sessions.save(..., expected_fingerprint=...)` 为后续 seal 提供可选的
冻结文件上传合同。默认 None 路径保持原行为，未自动为现有 Worker
启用严格模式。必需字段为 dev/ino/uid/S_IMODE mode/size/完整小写
SHA-256，可带 mtime_ns/ctime_ns；绝对路径及所有祖先 nofollow。
同一 unbuffered FD 在压缩前、压缩后、上传后完整核验，实际压缩输入
另做 hash/size；持续 tick。内容、inode、权限、路径或祖先变化、租约
失效都不得 commit，已上传未提交分块可保留，不重试、不删除、不清
guard。33 项新 fixture 通过；并非原生 goal/history 独立 seal，不
声称防同 UID 恶意并发写入或本地检查与远端 commit 的跨系统原子性。
缺少 nofollow/openat 的平台只拒绝该严格实现，节点可按合同自行适配。

首版 `e229651` 冻结源码虽在本机/VPS 各通过1508项，但开启 opt-in
的零模型 idle 检查遇到 pre-start 全局通知，误把未定义 thread 当作
本线程并访问未初始化 tracker。失败证据保留，不能作实机成功证明。
现已在 constructor 初始化 trackers，并在任何 thread 尚未知时忽略
全局/异线程通知；pre-start host 请求仍以固定 unknown 拒绝，不执行
回调。最终冻结 `58fac34` 本机/VPS Python3.6.8各1510项完整通过
（138.158s/142.777s），[公共CI](https://github.com/gih10012/personal-assistant-mesh/actions/runs/38038923724)
实际success；同版本 opt-in idle 在native0.162/0.159.2均initialize、
loaded list空、自然0/reader回收，无强杀。没有start/resume/model/goal
写入；正式核心代码/PID保持3198c87，没有部署或启用候选。

`PAM-007c-3` 已实现默认关闭的独立 yielded seal，不放宽 terminal seal。
配置 `native_goal_yield=true` 必须同时选择 verified Unix 和
`native_goal_drain=true`；这不是正式配置的建议启用指令。TERM 前先
保留实际 goal baseline 和 owned 原 rollout 有限前缀，允许 writer
追加及后续半行；捕获/意图 ACK 过期不能再发送 drain signal。
原运行器回收后，新 observer 只 initialize/read goal/read thread/loaded list/turns list，
不 start/resume/steer 或写 goal；前后核对原 thread 都未加载，注意
notLoaded 不等于原进程 idle。核对同 objective/createdAt、预算与用量
未重置、所有自然完成 turn 及稳定完整的 owned 原生历史；观察 RPC
也需短等 + tick，避免 lease 饿死。通过后才写 outcome=yielded、
runtime_closed/settled，原 goal 仍 active。若 goal 自然变成实际终态，
尊重它并走原终态路径，不写回 active。

`thread/read(false)` 后用 `thread/turns/list(asc,full)` 穷尽分页，拒绝
cursor 环、重复 ID、缺页和 summary/notLoaded；两版官方 schema
对缺省 itemsView 定义 full。只有明确 -32601 missing method 才尝试
实际 legacy full read，不能将超时/其它失败降成空历史。前后 metadata、
goal 和 unloaded 观察一致；独立 observer 自身亦须自然 0/EOF/reap。
[官方 goal 接口](https://learn.chatgpt.com/docs/app-server#manage-a-thread-goal)
与 [0.159.2 turn schema](https://github.com/openai/codex/blob/rust-v0.159.2/codex-rs/app-server-protocol/schema/json/v2/ThreadTurnsListResponse.json)。

`native_history` 只读同一 owned regular JSONL，检查完整字节前缀、
文件/祖先身份、全文件 hash/metadata 及本轮 start/natural complete；
支持官方 task/turn event aliases，error 或 abort 不能被后来 complete
掩盖。旧无关失败与 compaction 原样保留，不生成摘要。单条 record
超过16MiB或非支持布局只拒绝该严格 seal，不截断或限制原生能力；
缺少 nofollow/openat 由节点按合同适配。不是防恶意同 UID writer 的锁。

`PAM-007c-4` 的 Worker 接入已实现为 opt-in 源码，真实模型等待/唤醒
往返尚待验收。Worker 使用 seal 返回的冻结历史，不在 close 后调用
`native_rollout()` 产生新 RPC。原 guard 持续为 true，直到独立核验和
session/artifact commit 完成，才在原 task/epoch 的最终 update 中同时
写完整 yielded seal、false guard 和 waiting_children。children 提前
完成由既有 Store 唤醒原 row/new epoch，不另建 continuation task。
旧私有 stage-two unknown 不作为这个新合同的可重启测试数据。

严格分支 upload/commit 期间 task 和 native session guard 都为 true，
commit 必须明确 ok/saved/artifact_saved/scope；Store additive ok ACK
保留旧字段，但旧 server 无此 ACK 时本分支不能静默放行。最终同 epoch
update 也须 ACK；回复丢失可能已经写入 authority，必须读回原行对账，
不靠本地布尔断言或重试 finalize。自然 terminal 优先真实 status，不
因 wait_children 隐式续跑。这里只证明受控 Codex admission/历史边界，
不声称已停止任意 detached Shell/MCP 外部效果、达成全局 HA 或入网 Chat/Cloud。

首份 `eb760b0` 已公开且本机冻结1591项/CI通过，VPS同归档首次1591项
有3个失败，保留而非列双端成功：临时源码目录默认umask产生group-write，
MCP launcher正确拒绝；另两处快速同大小改写fixtures依赖文件时钟精度。
后续修正只让夹具显式推进mtime/接受实际prefix hash拒绝，并使用私有
验证目录，不放宽生产权限/历史检查。observer shutdown也不再单独
写unloaded证据，只由实际前后loaded-list读取生成。

一次新隔离 canary 的私有回调将官方 `tool` 字段误写为 `name`，在
排空前拒绝，专用进程最终强杀；原任务/thread/active goal/guard均保留
unknown，未resume、未清除、未变成成功。它不是正式服务或本版yield
执行成功；后续新合同验证不得冒称修复这条unknown。相关私有记录不进公开仓库。
