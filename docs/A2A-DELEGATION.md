# 远端 agent 委派与原 Leader 续接

终端之间有两个独立通道：`mesh-a2a/1` 承载 agent 协作，SSH 等 remote execution 承载目标机器上的终端操作。SSH 连接成功不代表那台机器运行了可委派的 agent，命令退出也不证明分布式任务完成。这里实现的是本项目私有协作协议，**没有宣称与 A2A 标准兼容**。

算力、性能、模型、工具、连接路径继续由开放能力描述符发现；能力目录不是模型的终端白名单。固定的是提交权限和状态一致性：谁能委派到哪个已授权 peer、哪个仍活跃的任务租约能提交、哪个认证结果能完成哪个 child。模型仍可选择任务、项目、agent 身份标签和分工。

## 一个账本，不另起一个任务系统

```text
原 Leader 的 tasks[parent]
  └─ tasks[proxy child, waiting_remote]
       ├─ remote_delegations: peer + message_id + parent_epoch
       └─ mesh_deliveries: immutable message + acceptance receipt
                         ↓ 认证的 mesh-a2a/1
                 目标节点自己的 tasks[real task]
                         ↓ 同 receipt 的认证结果查询
                 原 proxy child 收到实际结果
                         ↓ 所有 children 已终结
                 原 parent 续接原 native thread
```

`assistant_mesh.remote.Remote` 只增加关联映射，实际父子任务仍在原有 `tasks` 表。`mesh_children` 能看到远端 proxy child，`mesh_wait_children` 与本地 child 一起等待。`waiting_remote` 不是本地可领取状态；proxy 另带 `mesh.remote.proxy` sentinel requirement，以免错误状态转换使它在本机运行。

### 提交

接口：

```python
remote = Remote(store, network)
receipt = remote.delegate(task_id, node, epoch, call_id, peer, arguments)
```

`arguments` 接受 `input`、可选 `project_id` / `agent_id` / `role`，也接受 `project` / `agent` 名称；同一请求不能同时提供一组别名。项目与 agent 标签不限定枚举，不接受 `context`、`node`、`token`、`required` 等越权字段。

HTTP/运行时调用者必须先从私有部署配置验证 destination grant；`Remote` 不从模型提供的 arguments 推导授权。随后在同一 `BEGIN IMMEDIATE` 事务里：

1. 检查原任务的 `running`、node、epoch、deadline；Leader 任务同时检查当前 Leader 租约。
2. 创建原任务的 `waiting_remote` child、`remote_delegations` 映射、持久 delivery 和 `agent_actions` 幂等 receipt。
3. 提交之后，独立 Node transport 才能发送这个 immutable message。

相同 call ID 与相同内容只创建一个 child 和一条 delivery；内容或 peer 改变则冲突。已失去活租约的旧模型进程不能通过重放 receipt 或换新 call ID 提交远端任务。`queued: true` 只表示进入持久队列，不表示目标已接受或已完成。

目标节点使用它自己的持久账本和 native agent 连续会话；不把远端代理 child 当成原 Leader thread。断线后目标可以继续已经接受的本地任务。发送结果未知时，只能重发同一个 receiver 强制幂等的 message ID / body；不创建另一任务，也不把此规则推广到 SSH 命令或不具有 receiver 幂等契约的工具。

### 回收结果

接口：

```python
remote.reconcile(peer, message_id, remote_task_id, status, result)
```

这个方法只能由服务自身拥有的 Node result-polling 调用者使用，不能直接暴露成任意 worker、模型或 report 的完成接口。每次检查：

- `peer + message_id` 对应已有远端 child 映射。
- delivery 已 `accepted`，其持久 receipt 的 protocol、ID、内容 fingerprint 和 task ID 均吻合。
- 结果来自该已认证 destination 的 owner-bound task 查询，且 remote task ID 与 receipt 相同。
- 返回状态属于已知账本状态，结果是最多 65,536 UTF-8 bytes 的文本或 null。

`running` / `waiting_auth` / `paused` 等观测只更新原 child 的 checkpoint/result，不产生另一个执行请求。`completed` / `failed` / `needs_review` 是真实远端终态；最后一个 child 终结后，只有原本 `waiting_children` 的 parent 才变回 `pending`。`needs_review` 不是成功证明，父模型必须检查实际状态和证据。

原父任务租约已经过期、Leader 已换节点，或父任务仍在本轮运行，不妨碍接收一个由持久委派授权的合法结果。若父任务被用户暂停则不自动唤醒。快速远端完成发生在父任务 yield 之前，也不会丢失唤醒：原 `Store.update_task(waiting_children)` 会检查实际 children。

父任务恢复时仍使用原 `task_id` / native scope，取得已有 native session 和 `children.result`。不会为了交还结果创建第二个父任务、复制一个新的 Leader，或用应用生成的摘要代替原生 Codex 连续性。

完成记录不可逆；较早的 active poll 不能覆盖已完成的 child。重复的相同终态是幂等观测，不再次唤醒。相矛盾的终态/结果报冲突，不能靠一份新的 report 偷改已确认结果。

`Remote.reject(peer, message_id)` 只对同一持久 delivery 的**首次提交明确权限拒绝**收束 proxy 为 `needs_review` 并通知原 parent。它检查 `denied`、`attempt == 1`、固定拒绝原因和严格布尔 `delivery_not_accepted: true` 证明；unknown、401、超时都不是未交付证明。尤其是“首次对方已接受但 receipt 丢失，第二次重试因撤权收到 403”，原任务仍可能执行：该情形不收束为未交付，不创建替代任务，继续等待权威核对。重新授权也不意味着可以换一个新 message ID 盲重放。

## 与自主工作和 report 的边界

没有映射的普通 operator raw-queue job 可以被 Node 观测，但 `reconcile` 返回 `not_remote_child`，不修改父子任务。目标自主工作和 unsolicited report 同样只能追加证据，不能靠 payload 中任意 `task_id` 完成另一个任务。自主节点不能因失去网络便以局部租约接管全局 Leader。

暂停原 parent 只阻止它自动续接；这不是已经实现的远端实时中断协议。当前控制面拒绝直接 pause/resume 一个 proxy child，避免本地恢复误改成 runnable 状态或冒称远端已中断。取消/恢复远端运行、转移同一个远端 agent 到第三台机器、跨 authority 的 native rollout 迁移仍需要各自的受控协议，不能把此结果映射当成已经覆盖。

## 验证范围

`python -m unittest tests.test_remote -v` 覆盖 34 项 bounded SQLite 验证：提交/完成/拒绝事务回滚、并发重复调用、任务与 Leader fencing、混合本地/远端 children、快速完成、父暂停和换 Leader、拒绝 proxy 本地执行控制、原 native session 保留、错误 peer/task/receipt、未知 transport 结果、首次拒绝与未知提交后拒绝的区别、严格布尔拒绝证明、结果大小、未授权 report 和不可改写终态。

这些测试证明账本与映射契约，不等于真实两台机器的 model 推理、网络中断恢复、离线模型可用或跨机器原生迁移已经验收。HTTP、Node polling 和旧 native thread 的 CLI handle 仍须正确接线，并以真实运行验收。
