# 原 receiver 的 Cloud owner runtime 薄接线

本片基于 `22bba5c`，分支 `work/cloud-owner-runtime-20261019`。没有生产、
GUI、模型、Cloud submit、凭据注入或新增费用操作；完整 Goal 未完成。

新增 `cloud_owner_runtime.py`，只提供 owner 安装与可关闭的 loopback 组件。
它使用原 `api.ingress`、`api.ingress_results`、`api.store`；不创建 Store、
恢复旧状态、修改原表结构、创建/claim/续租任务或建立 keeper。原 Claw
收发器和 Chat source/route 不替换。已有 authority 的可选 conversation
receipt 表仍由原 ConversationAuthority 初始化，不新增任务/租约表。

## 安装与启动严格分开

1. Root 在现有私有目录保存 [owner config](../deploy/cloud-owner-runtime.json.example)，
   配置文件0600、安全父目录0700。所有占位符必须由真实 owner 状态替换。
   两个 `expires_at` 填同一个实际短期未来 UTC 秒；模板0故意不可安装。
   `task_holder` 是实际 Store claim 的 worker；`mesh_task_id/mesh_epoch` 来自
   同一个真实 running task。文档 PAM ID、进程 PID、Cloud provider ID 或
   假任务均不适用。该真实工作的自然生命周期必须覆盖需要授权的连接；
   不能为接线新增占位工作、修改 scope、保活或延长 claim。
2. 私有配置中的 `lease_id/request_id` 是原 owner grant 映射。validator 比较
   完整原 grant，并在原 Store 事务中调用 `_task_is_live(db, task,
   node=task_holder, epoch=mesh_epoch)`，核实际 deadline/Leader generation。
   `enrollment.owner_binding/authority_id` 必须等于原 ingress。没有真实
   claim 或租约过期，安装/启动返回 `not_prepared/original_task_not_live`，
   不创建 journal、替代 task、授权或 listener。
3. 仅在明确 owner 安装操作中调用
   `install_owner(api, load_config(path), authorized=True)`。它执行一次受权
   `CloudAdoptions.adopt`、`CloudMembership.prepare`，创建独立私有目录，
   返回非秘密 bootstrap。credential 只接受已经单独授权/注入的 SHA-256；
   不接收 token 明文、operator bearer 或模型 key。重复安装保留原身份与
   immutable grant，不覆盖旧 launch journal、unknown 或关闭的 credential。
4. 普通启动仅 `CloudOwnerRuntime(api, document).start()`，从既有 adoption/
   membership journal 读回原 grant/hash，缺项不自动 adopt/prepare。grant、
   request、lease 或 credential hash 变化不能当重连。正常 receiver 的
   [ready/finally 示例](../deploy/cloud-owner-runtime.py.example)保留全部原 hook。

`adoptions_directory` 保存 `cloud-adoptions.sqlite` 与 `node-artifacts/`；
`conversation_directory` 单独保存 Cloud `conversation.sqlite`，不使用 Chat
journal。Cloud ID 可在准备配置时纯计算：`cloud-adopt-` 后接
`_digest([owner_binding, mesh_task_id, request_id])`；节点 ID 使用既有
`cloud_membership.node_id(job_id)`。这只计算身份，不建立任务或 provider 证明。
安装时完整 CloudMembership/enrollment validator 再核原节点与合同。

## 最短公网和双向链路

组件固定监听 `127.0.0.1:loopback_port`，只接受 `/cloud/node`。`0` 允许
隔离 fixture 由内核分配端口；正式配置选明确空闲端口，并把既有 TLS
owner 配置的 `cloud_port` 设为同值。原 `mcp_port/oauth_port` 不变。
继续使用现有 TLS renderer 的 Host/XFP/POST/body/no-retry 边界，不公开
`/v1`。没有新增 ASGI/SDK 依赖或第二 poller。

Cloud source 固定 `openai-codex-cloud`，subject 固定原 `node_id(job_id)`。
内部 backend、service 和 bridge 使用同一固定 principal；bridge 的私有
client 只直接 dispatch 原 conversation route，不需要 operator credential，
也不对原 Chat authority 再调用 `attach`。question/steering 仍核实际原
ingress request、task/epoch/interaction，不能将普通文字变为原生 approval。

Cloud grant 的读 scope 为 `task:read`，内部 ingress principal 为
`tasks:read`。示例额外授予 `tasks:submit` 支持 task_submit/open/reply；
没有 gateway、能力调用或 task_continue。Cloud `task_submit` 的 envelope
request ID 成为原 ingress ID；会话 arguments.request_id 必须使用它，
不能误用 enrollment request ID。hello、写请求、message/cursor 均保留
各自原身份；未知只查既有 receipt，启动/关闭不重投工作。

`close()` 停止并关闭自己的 listener/thread，可重复调用；不撤销 grant、
取消远端任务或证明远端静止。显式 owner credential 撤销仍用原
`membership.close(..., authorized=True)`；任务换 epoch/过期后新写入即
被原 Store validator 拒绝，不消耗新的 effect intent。
`status()` 分开报告 listener 是否 serving 和实际 grant/claim 是否 prepared；
lease/credential 失效不会因仍有 listener 线程而被称为准备就绪。

## 检查与真实剩余

新增小组使用隔离 SQLite 真实 claim、实际 loopback HTTP、原问题回答与
receipt，检查缺安装/缺 claim、原 holder/epoch/deadline、私有 lease/request/
hash 绑定、撤销后重启、不重建 Store/adopt/prepare、固定 Cloud source、
原 Chat hook 保持和正常关闭。provider 和 credential 均合成；这不证明
真实官方 Cloud 来源、主账号登录或生产交互。Py3.6 grammar 检查不等于
已在宿主3.6.8运行。现代 MCP SDK 小组由 Root 既有隔离环境另核。

Python3.14.7 上 `tests.test_cloud_owner_runtime` 8项加原
`tests.test_cloud_adoption` 5项共13项，1.434s、0skip通过。首次新fixture
因 enrollment 与业务默认同 native scope 而未能 claim；仅将隔离fixture
真实 enrollment work 配置为独立 scope，未修改生产组件或原 Store guard。
本机没有 Python3.6 可执行文件，未称宿主3.6.8实跑。

Root 仍需独审/安装冻结导入闭包，找到真实有效的原 claim（缺项如实保持
未准备），执行明确 owner 安装并串行接 TLS、私有 bootstrap/secret。
随后由本人原官方 Cloud 任务自身连入、做实际双向问答和完整成果读回。
provider_identity_verified 初始 false；只有既有独立正常 UI verifier 可
提升 provenance，runtime 不把监听、bearer 或 HTTP200 当官方执行证明。
