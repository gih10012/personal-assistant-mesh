# 原 Claw 客户端连续性与独立通信 companions

2026-10-14 开始，2026-10-15 沿原额度中断线程收尾；
`work/claw-client-continuity-20261014`，base `0b1c146`。
此片是**源码与实际 SQLite/loopback HTTP 隔离验证**。没有安装、生产
停机/冻结/迁移、grant、新凭据、微信/model 请求或手机送达证据。
原 Worker/Store/native/source pins 不被整份 HEAD 更换；Root 串行切换。

## 原逻辑地址不是新物理地址

旧独立 CLI 的 `remote-sends` fingerprint 包含原 `control_url`、endpoint
及完整 payload。换这个地址或清掉记录都会破坏已 attempted/unknown
身份。因此仍保留原 remote.json 的 control_url/token_file 和全部
remote-sends/remote-uploads/remote-read-cursor，**只增加**私有
`mesh_communication` 物理路线。

[示例](../deploy/communication-owner-client.json.example)不是可直接启用
的实际 identity/token。Root 填入外部核验的 communication_id、原逻辑
URL，以及固定新能力入口/独立窄角色凭据引用；不改变宿主代理或17680
全局隧道，不把 owner/operator bearer 交给任务或官方 Cloud VM。

[构建器](../deploy/build-claw-client-overlay.py)只生成 NEW source，接受已
审查的旧 `ilink_remote.py` SHA
`3c172a06f0d412ed930742cb7b56b114468368479329e2394b1414b956c8387e`，
原 submit/status/media-upload/journal 算法保持，只插入两处显式 opt-in
hooks：request 选择物理通信入口；run 在 upload/submit 前 preflight。
未配置时仍用原逻辑。输入文件不改；输出不安装或修改实际账号。

```sh
python3 deploy/build-claw-client-overlay.py \
  --source /PRIVATE/inspected-old-skill/ilink_remote.py \
  --destination /PRIVATE/new-claw-client-source
```

Root 必须选择实际独立 CLI release 的模块加载位置、固定这片候选
assistant_mesh import 路径、备份原模块后再安装；只有目录存在不能算
接线。旧模块变化/hash不匹配先审查新版本，不能直接硬覆盖。

新 broker 的 `owner_client` 角色只允许旧 wire：GET `/v1/status`，POST
`/v1/inbox`、`/v1/notify`、`/v1/notify/status`。角色配置需绑定原
owner_ref、origin_authority、legacy_control_url；请求再检查固定逻辑
origin 与 communication_id **before enqueue**。不继承 task/worker/
executor/activation/grant/operator 路由。status 的 leader=null 明确是
通信-only authority，不伪造另一个 Leader。身份冲突不 fallback裸iLink。

旧 attempted send 仍先走原本地 fingerprint 比较，**只有查询**已迁入
broker 的同原 outbox ID/status/detail；原 client_id、unknown、media
引用和 notify receipts 不变。原记录查不到/连接不通仍unknown，不
重POST/改ID。只有没有原 reservation 的新输入才可提交一次新通知。

新媒体在 upload、reservation 或 HTTP request **之前**返回明确
`BOT_REMOTE_MEDIA_UNSUPPORTED`，不伪装成文字。原已reserved媒体
核原上传的kind/name/content fingerprint与原submit payload，然后
仅查原媒体outbox，不重新上传/发送。Producer 的 text-only relay也
在新增队列前拒绝media；原媒体行仍可查回，原生媒体工具不被阻断。

## 原归档分页序号

Transfer 升为 `mesh-communication-transfer/2`，必带 `inbox_order`，
保存原 inbox rowid 对应的 message_id/sequence，包括非连续序号。
新独立 DB 的实际 rowid不作为旧 cursor。新消息在同canonical事务
追加 max(sequence)+1；原 cursor继续按 sequence筛选，重新迁移也
保持映射。prepare/verify逐行核实际映射，缺/重复/改序号拒绝；不能
凭prepared marker或更早SHA忽略原分页。旧v1 artifact没有序号证据，
明确不兼容，不自动推断“反正现在本人cursor为0”。

`tests/fixtures/claw_remote_original.py` 是上述已审查 owner-controlled
旧 CLI 模块的完整源码 fixture，无账号/聊天；其SHA由构建器核对。
验证不是用另写的短 sender 或假journal来冒充原客户端。
来源为本人 `ncut-wechat-skills/skills/wechat-personal/scripts/ilink_remote.py`；
原 MIT notice 保存在相邻 `claw_remote_LICENSE.txt`，生成overlay也附原license。

## Task inbox 与 notify 独立接线

[`communication_runtime`](../assistant_mesh/communication_runtime.py) 提供
owner-installed companion，不持有account，不启动iLink。输入来自
authenticated broker executor；沿原 `CommunicationInbox` 调用精确
旧 `_ingest_in_db`，原 message/task/receipt同一个事务。一次prepare/
target持久化后发生未知只查原receipt，不再次prepare/deliver、不
改authority；receipt ACK丢失可沿同身份回查。

已attempted旧输入和control固定原authority；无关的新普通输入仍可
派发，pending优先尚未attempted记录，避免一批旧unknown挤占窗口。
多个executor的目标仍由broker原writer事务决定，不靠两份本地lease。

默认 ingest保留required=[leader]，Cloud原Leader连续路径不改。本机
原嵌入Worker没有leader能力，不能仅把消息写进本机DB就说已经能跑。
可选**可信owner配置** `new_task_route` 只对原inbox/task不存在、broker
kind=message的新输入，同事务设置 agent+mesh.node:<本authority>和
稳定项目/native_scope。原 /goal、/plan context保留；旧controls/tasks/
receipt/unknown/native记忆不迁移或改写。第二新任务复用相同本机项目
scope，但不是冒称复制了VPS Leader全部记忆或接管全局Leader。

[本机示例](../deploy/communication-companion.json.example)默认
accept_ordinary=false。Root可让正常Cloud companion接普通输入，确认
VPS停止后再明确启用本机；已attempted的旧输入绝不能随开关改投。
Cloud同原Leader路径省略new_task_route。启用这片CLI只在已核实际
原task DB/owner config上使用 exact-source pin/no-recovery构造。

Notify companion复用原 `NotificationRelay`，固定 source_node、原逻辑
peer/authority/receipt route，与新的**独立 notification Client**组合。
它不使用或修改Node.peers里的A2A Client。Broker保留 origin=cloud
不意味着物理host还是VPS；新物理入口可以在laptop或由现有加入路径
承载。旧本机accepted relay及private/channel pending不补造发送权。

本机Node本身已经拥有一个旧relay线程。Root在实际idle边界停止旧
承载后可选 [`communication_node`](../assistant_mesh/communication_node.py)
[启动适配](../deploy/communication-node.json.example)：Node引用 private/
no-relay策略，embedded API引用完整producer策略。两份server配置的
所有非notification字段必须全等；原Node/A2A/Worker类与其Client不改，
Node构造显式no-recovery，启动线程前恢复factory。如此旧relay不启动，
但新API的explicit/implicit通知仍生成原relay行，由唯一companion承载。
不能仅删除owner_relay让新explicit通知变private，也不能改共享A2A URL。
真实唯一flock/线程与native连续性仍须Root核验，不凭模板算完成。

Root以当前SSH/HTTP等适配提供VPS→broker可达路径（例如独立loopback
转发，不移动整个原17680）。示例fixed Client范围和current Linux
flock只是现网实现；协议不要求所有Windows/手机节点照抄。

## Root 串行 cutover 边界

1. 检查/冻结包含本片的候选；原沟通group所有callbacks真实归还，停
   原native-refresh bridge的已admitted调用并核原请求；所有旧 channel
   writers切换为producer-only后才取最终export，防旧raw receiver重启。
2. 原execution DB的channel-only write barrier用于最终export/activate
   窗口，不停无关Worker/native/A2A。迁移完成后原API已无account IO；
   新 `CommunicationInbox` 会向该DB写**任务投影** inbox/context/cursor，
   不可永久冻结这些列再宣称VPS能接新消息。其canonical状态仍只有独立
   broker的DB，projection不变回第二channel authority。
3. 外部保持transferSHA，NEW私有prepare/实际verify、真实source/external
   closure callable激活、单一witness/carrier grant/start仍由Root执行。
   本片构造/loopback测试不提供这些生产proof。
4. 配新窄owner_client peer及旧CLI opt-in物理路线；原journalURL保持。
   配独立executor/notifier Clients和原task producer，不改变A2A origin；
   默认未选普通输入，先验证原状态及完整未知身份。
5. 一个新明确owner消息→真实选择authority→原task/native执行→原通知/
   完整回执；确认VPS停机时本机新任务、通信仍可用。旧控消息继续排原
   属authority、unknown不重投。重联禁止旧poller、第二witness、DB回灌。

当前测试涵盖 exact旧CLI回查/ACK丢失、origin/body/role约束、media零
新副作用、非连续分页/重迁移/实际映射篡改拒绝、local同事务/ACK恢复/
项目scope、旧control归属/unknown不重投、独立notify Client和producer
拒绝media。账户、grant/quiescence proofs与iLink transport均synthetic，
无手机/模型/生产handoff验收，也不宣称任意网络分区无缝微信HA。

最后收尾 **31项/5.494s、0 skip** 通过（15项新客户端/运行接线＋16项
独立通信既有测试）。包含新freeze候选的真实 original Node/embedded
Worker/HTTP启动、A2A hello、无旧relay、no-recovery原submitting保持，
以及独立companion实际接纳/notify客户端接线；全部task/proof/账号均
synthetic，原Worker无可领取task/未启动模型，notify未调用iLink。

中断前相关253项/22.344s通过，旧Store ResourceWarning保留；此数字
不覆盖随后新加Node启动测试，也不是新head全量/CI。原test handles
恢复时已不存在，且无残留测试进程；没有重启已完成的253组。
新启动测试首轮因测试的port0与worker URL17681不匹配明确失败；只修
fixture URL为相同port0，原检查和生产代码未放宽，独立复跑通过。
