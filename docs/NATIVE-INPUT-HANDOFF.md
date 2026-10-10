# 原生 goal 交接前置合同（PAM-007c）

2026-10-10 源码候选。它补齐输入回执、可选传输和 scope 归属，**没有
启用正式 active goal yield**。运行期仍为冻结 `3198c87`。既有私有
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
本人的 0700 临时目录和 0600 socket，原有 auth/memories/plan/权限
配置保持。没有 TCP 公网监听、第三方依赖、新 key 或新费用。缺少
AF_UNIX 的环境明确不支持该选项，节点可以继续 stdio 或自行适配
相同生命周期合同；Linux 机制不是所有平台的限制。
[官方 listener](https://learn.chatgpt.com/docs/developer-commands#codex-app-server)

传输校验升级握手、帧、JSON 与上限，接收超时保留部分帧。写入失败
可能已被服务端接收，因此不会自动重试。关闭只向自有 app-server
PID 发一次 TERM；超时清理若必须 KILL，或者 reader 强制关闭/退出码
非零，不能通过 goal terminal seal。清理本身不证明协同 yield。

**还没有**在 finish 内保持 heartbeat/请求响应的 admission drain，
也没有独立不加载 thread 的原生 goal/history readback。当前 active
goal 等子任务仍返回 `codex_goal_coordination_yield_unavailable`，保持
原身份和 guard；不能因 socket 可连接就放行。

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

新增测试是离线协议/状态 fixtures，不是模型已收到输入的真实证明。
继续推进：正式接入 drain event pump 与独立 yielded seal；原 task
等待/唤醒/同 goal 输入的真实 roundtrip；连续原生历史 checkpoint 和
长 RPC lease watchdog。之后再加载正式控制器与 3–5 天前沿检查。
Chat 模型 tools/OAuth 与 Codex Cloud job/node 仍单列未接通。
