# 通信状态/仲裁独立故障域候选

PAM-009c/d，2026-10-14，**源码＋隔离验证，不是已迁移或生产 HA**。
负责原专员 `/root/claw_minimal_fenced_runtime`；分支
`work/claw-state-faultdomain-20261014`，worktree `claw-state-faultdomain`，
base `ec14fb741e50fee505872673b1590404439a1a15`。Root 合并后独占执行
生产 cutover；本分支不写主协调文件、不停服务、不 grant 或真实收发。

源码候选使用 [构建器](../deploy/build-communication-candidate.py)，复用
已验证的旧 receiver 40模块组合，不复制 HEAD Store/Worker：38原模块
字节保持，仍只两项既有最小 Store/server patch，再加入通信 sidecars。

```sh
python3 deploy/build-communication-candidate.py \
  --base-root /private/exact-old-receiver \
  --destination /private/new-communication-source
```

`communication-candidate.json` 记录实际源码 SHA；构建/静态 import 不
打开账本或运行 transport，更不会准备真实迁移数据。Producer 原生
pin 检查与独立 broker 都能在同一候选中使用，但仍是不同 DB/服务职责。

## 本片不再只搬 carrier

此前 carrier-only 放在 laptop，原 canonical Store/witness 在 VPS，VPS
不通仍无法记录游标或发送。这次提供独立
[`CommunicationStore`](../assistant_mesh/communication_store.py) 与
[`communication_broker`](../assistant_mesh/communication_broker.py)：

| 内容 | 独立通信 DB | 原执行/task DB |
| --- | --- | --- |
| 原 inbox、cursor、owner context、receiver incarnation、outbox/client ID、notify receipts | canonical，完整保留 | 原行保留但不再作为新 channel writer |
| ChannelState 完整原 outcome/receipt、CarrierWitness 任期/once-only intent | 同一个独立 writer transaction | 不复制成第二个 writable witness |
| 历史 task/native session/执行效果 | 只保存 authority/task ID、原 epoch/state/row SHA 引用 | 仍是原业务和连续原生历史的权威，不迁移/重放 |
| 新 owner 消息 | 原消息 ID、完整私有 body、耐久交付队列 | 明确选定 authority 后按原 ingestion 规则接收 |
| 未决发送或已 attempted 的交付 | 原 ID/target/status 保持；不换 ID 或自动重投 | 用原回执读回对账，不凭新节点创造一次执行 |

复用已有 `ChannelState`、`CarrierWitness`、`CarrierFence`、
`RemoteChannelState`、`RemoteNotifications`、owned supervisor/IPC；不重写
iLink SDK、授予规则或未知效果语义。独立 DB 没有 `tasks`、`leader`、
`native_sessions`、`session_chunks`、`steering`，不能被当成 Leader 工作区。

为兼容原 ticket/notify receipt 保留 `origin_authority` 标签（现网历史值
为 `cloud`）；它不是把 Alibaba 的 node ID 改名/迁到 laptop。Broker
报告明确区分 `physical_host=laptop` 与 `origin_execution_authority=cloud`。
新的通信服务能力 provider 应是实际承载节点；官方 OpenAI Cloud 另算。

## 新消息如何让本机继续工作

Poll 的原 canonical transaction 同时写完整 inbox、context、cursor、
`communication_events` 和原 receipt。旧迁入 inbox 只用于 dedup，**不建
可执行副本、也不重新生成交付事件**。历史 task 保留引用而非假装已经
复制全部执行依赖/native 记忆。

新普通消息可由 broker 的 owner-bound executor 选择当前可用 task
authority，例如 VPS 正常时选原 Leader，确认 VPS 停止后选本机。
`prepare` 在实际接纳前持久化原 target/post intent；attempted/unknown
不能改投另一 authority。无关的新消息仍可选择本机，不被旧 unknown
整体阻断。旧 `/pause`、`/resume`、`/answer` 等控制消息固定回原 task/
interaction 所属 authority；离线就排队，不在另一份 DB 中操作同名任务。
项目 executor 可注册自己真实 task/interaction 引用，不能覆盖旧归属。

[`CommunicationInbox`](../assistant_mesh/communication_inbox.py) 用原 Store
的 `_ingest_in_db` 与独立接纳 receipt **同一个事务**：保留原消息/task
ID、goal/plan/control/effect guard 规则，不自行重新实现业务解析。
丢失 ACK 后只查该原 receipt，不再次调用 ingestion。仅接纳确认，
`execution_verified=false`，不把持久 task 当模型或工具实际执行。

这一 inbox adapter 要求已发布的最小 ingestion extraction。现网原
3198 Store 没有该 helper，Root 应使用原 frozen Store＋现有 patch 的
独立 API/ingress 覆盖层，**不能换整份 HEAD Store 顺带升级 native goal**。
本测试也加载精确原 Store＋extraction，而非拿新 Store 冒充兼容。

## VPS 恢复后，通知如何不再依赖 VPS channel DB

Broker 实现原 `/v1/mesh/notify` 与 `/v1/mesh/notify/status`；原 route ID/
receiver incarnation/source-node/request ID/fingerprint/receipt 均保持。
现有 `NotificationRelay` 可直接使用它，不另造发送器或重复 POST。
同一原 unknown notify 查回仍是 unknown/client ID，不重建 pending。

[`communication_producer`](../assistant_mesh/communication_producer.py) 是
Root 显式选择的原 task-API 启动适配器：禁止 iLink account/carrier，
沿原 no-recovery 构造边界，只有原 `_enqueue` 的选定 subclass 把隐式
通知交已有 `mode=private + owner_relay` 的耐久 relay。已存在的旧 outbox
原行/客户端身份不改，不给它们补造新的 relay 重发送权。明确 private
输出仍可 private；Worker、native adapter 和其余 Store 方法保留。
入口检查旧 receiver 的固定源码 pin，拒绝意外加载 HEAD native Store。

Root 需配置实际可达的 broker 能力地址、已有授权 source-peer 凭据及
relay companion；调整仅针对此额外沟通能力，不修改宿主全局代理或
封禁任何 native Shell/网络。旧 attempted relay 的 peer/authority/route
仍按原记录对账，不能因改配置把它当新请求。原客户端的 endpoint/
角色映射也需由 Root 核对；配置模板或地址不是连接证明。

## 准备/激活严格分开

[`communication_migration`](../assistant_mesh/communication_migration.py)
提供 `export`、`prepare` 和实际候选核对 `verify`，**没有 CLI activate/grant/start**：

```sh
python3 -m assistant_mesh.communication_migration export \
  --database /private/original/ledger.sqlite --authority ORIGINAL_AUTHORITY \
  --owner-ref OWNER_REF --account-binding ACCOUNT_BINDING \
  --transfer-id ORIGINAL_CUTOVER_ID --output /private/transfer.json

python3 -m assistant_mesh.communication_migration prepare \
  --transfer /private/transfer.json --expected-sha256 EXTERNALLY_RETAINED_SHA \
  --database /private/new-communication.sqlite

python3 -m assistant_mesh.communication_migration verify \
  --transfer /private/transfer.json --expected-sha256 EXTERNALLY_RETAINED_SHA \
  --database /private/new-communication.sqlite
```

Export 用 `mode=ro/query_only` 单一 BEGIN，不构造 Store/recover，不读认证/
附件/原生 rollout。它不是把旧 audit checkpoint 的 `migration_ready`
改成 true。选定 channel 行完整导出；业务/native 仍是原 authority 引用。
现有 fence 必须完整、仅一个绑定 account 且 frozen；partial schema/
未冻结源拒绝。后续独立 broker 迁移也保存 queued/attempted/unknown/
confirmed 消息及原 target，不重新排队。

目标只能是**新的**0700目录中的0600文件，不覆盖既有 DB，不复制原
credential。Prepared DB 未激活、无接收器；现有 witness/incarnation/
epoch/unknown intent 完整保留；全部导入完成才置 prepared marker，半写
候选不能服务/激活。固定 verify 逐表核实际 inbox/outbox/meta、事件、
原 task/control 引用、relay provenance 及 fence against 外部原 artifact，
unknown→pending 或原 target 修改不能仅凭原 marker/SHA 通过。缺 verifier
仍不能 grant。导出格式含
私有聊天/context，不入公仓或 capability 广播。SHA 只验证内容一致，
不是源冻结或外部 quiescence 证明；不能拿自身 SHA 自我授权。

Root 通过 `activate_prepared(..., transfer=externally_pinned_original_artifact,
owner_verifier=trusted_actual_callable)`
提供原 communication identity、externally retained import SHA、proof ref
精确绑定的 `mesh-communication-cutover-proof/1`，需独立核验：旧 channel
writers stopped、原 canonical writes frozen、external operations quiescent、
原身份保留。JSON booleans、PID消失、network timeout/TTL 都不提供该
callable。激活也**不授予 carrier**；原 CarrierWitness grant/reconcile
仍需自己的真实 owner verifier，unsettled intent 不会被忽略。

## Root 的受控应用顺序

1. 审查本分支 commit/隔离回执，冻结 standalone broker 安装；为原
   task-API/ingress 使用原 frozen Store＋ingestion patch/producer wrapper。
   只读核当前原数据/schema、原 task/native/unknown 和通信来源，备份。
2. 在实际安全/idle运维窗口关闭**所有旧通信 writer**：停唯一旧 carrier、
   观察同原句柄全部 callback 与原 outcome/canonical 状态，防它恢复；
   把后续 task 通知改为独立 broker 的 producer/relay（切换时暂停该
   relay 的网络派发）。不能只停 poller 后让旧原 channel outbox 继续
   有新写入，再拿更早 snapshot 当最终数据；不停止无关工作来伪证。
3. 从最终冻结的通信状态只读 export，外部保留 SHA；加密/授权交给
   laptop 新目录 prepare。独立核对原 cursor/context/完整 inbox/outbox/
   client IDs、notify route/receipts、task refs、原 unknown/fence。
4. 用真正源停止/在途/写边界证据激活；单一 owner 仲裁在 laptop 上
   授予一个受控 carrier。Broker 的本地 owner lock、任期及真实 IPC
   handles 只管这项共享接口，不是 host sandbox 或无限 HA 证明。
5. 验旧 holder 不再发新 IO；只一条新的明确合成 owner 消息→本机
   原 ingestion/task→真实模型结果→原 notify/client 身份→独立结果回查。
   然后确认 VPS 停止、原执行 DB 不可达而通信保存/本机新工作仍可用；
   原旧任务控制/unknown 保持排队，不凭断网重新执行旧业务。
6. VPS 重联只恢复原业务/历史 authority 与新 producer/relay；禁止
   启动其旧 channel receiver、复制独立 witness 成第二 writable authority，
   或用旧 DB snapshot 回灌消除新状态。其他 Chat/终端入口保持冗余。

如第2/4步真实 evidence/permission 不足，只暂停该切换，不把 prepared
DB、源码、local closure 或候选可达性称为交接完成。Root 的现网操作是
后续串行工作，本分支没有执行以上步骤。

## 本片验证/真实边界

14 个小型真实 SQLite/loopback HTTP fixtures：精确旧 Store；独立 DB
无 runnable/native tables；原 unknown/client_id/route 保存；原执行
DB不可用时新 poll 写独立状态；本机接纳原 ID与丢 ACK只读回执；旧
control 归属/新项目引用；unknown不改投；原 implicit通知走已有relay；
部分 batch 原子回滚；独立 broker 再迁移仍保留 unknown delivery；
激活拒绝实际行篡改和 incomplete prepared marker。
所有 iLink/account/proof 均 synthetic，不证明生产 topology、外部请求
quiescence、手机收件、连续跨节点 native memory 或整个 goal 完成。

当前分支14项约1.03s、0skip；同 channel/state/supervisor/notify/relay
相关聚合150项13.572s通过。最终候选按原冻结包构建并核静态import/
producer pins；Root 后续另核冻结CI与真实串行应用，不能以这些fixture
或源码 manifest 代替上述 cutover 验收。

当前生产只读 metadata 核对：唯一 receiver/原 Worker 仍原进程且
NRestarts0，通信原 schema 匹配、四项必需 meta 存在、DB0600，未部署
fence tables。只读样本 outbox 为144 accepted；该计数**不是冻结或
以后状态**，未读取私有正文/凭据，不拿它签 migration/grant。

原 task/完整原生记忆仍需其自己的可恢复持久 authority；本片故障域
脱钩的是通信，不复制 Leader。laptop/witness 本身失效仍关闭新共享
账号 IO；仅两节点的任意网络分区 seamless WeChat HA 没有被承诺。
Linux/Python/Unix/flock 是现有部署适配，不要求 Windows/鸿蒙/新设备
照搬；协议与原生自由仍按完整 GOAL 继续。
