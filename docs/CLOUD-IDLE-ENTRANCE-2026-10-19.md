# 本人 Cloud 自发入口：Leader 空闲也能提交

基线 `c46ef1e`，分支 `work/cloud-idle-entrance-20261019`；共享显式sync
helper独立依赖 `9e3d8f4`。本片新增入口
profile，保留此前 task-bound Cloud worker/profile 和冻结构建成果。
入口不能要求先有 Mesh 业务 task/claim；本人已经运行的官方 Cloud 任务
可用 owner 受限授权主动连入，再向空闲 Mesh 提交工作。它不创建/唤起
另一 Cloud job，不增加模型 key、OAuth 引擎、费用或 laptop 依赖。

## 两种身份各自保留

`CloudEntrance` 使用 `mesh-cloud-entrance/1`、独立
`cloud-entrances.sqlite` 与私有会话目录。它的 policy 仅固定 owner、
source=`openai-codex-cloud`、subject、派生 entrance/node ID、project 和
443 callback；没有 Mesh task/epoch/lease 字段。owner 显式安装短期
`mesh-cloud-entrance-grant/1` 及既有专用 scoped credential 的 SHA-256，
普通启动不安装、续期或撤销。模板 expiry=0 故意不能安装，需真实短期
未来 UTC 秒。credential 值仅在正常 Network secret 注入/私有状态。

旧 CloudJobs/CloudMembership、CloudAdoptions 和 CloudOwnerRuntime
均不修改：需要执行任务的 worker profile 继续核真实 task/epoch/holder/
lease，旧 launch/unknown 保留。新入口没有 capability call、任意 Shell、
worker/lease 管理、全局 task 查询、operator route 或原生 approval。

授权入口不是 provider attestation。hello/持久消息/成果均不提升
`provider_identity_verified=false`、`execution_verified=false`；provenance
保持 `owner_authorized_unattested_entrance`。独立正常官方 UI 观察与真实
模型执行仍分别记录，不能把 bearer、URL、HTTP200或消息保存当物理来源。

## Root 最小接线

1. 在原 Mesh 私有配置中安装专属 `role=ingress` peer，固定同 owner、
   source=`openai-codex-cloud`、同 subject，仅 `tasks:read/tasks:submit`。
   原私有 conversation authority 一次安装允许 Chat 与 Cloud 两个 source，
   例如 `attach(api, sources=('chatgpt-chat', DISPLAY_ID))`。已安装实例的
   sources 不能靠重复 attach 临时改变；由 Root 独占受控安装。仍不公开
   `/v1`，不把 GitHub/OpenAI/operator bearer 作为入口或内部 client。
2. sidecar 复用该既有 scoped Client，通过
   `RemoteIngressBackend(..., source=DISPLAY_ID)`、相同 backend 的
   `ConversationService` 及固定 principal 的 `ConversationBridge` 装配。
   `cloud_entrance.assemble(...)` 不创建 Store、不调用后端、不 claim task。
   只有客户端实际 `task_submit` 才在原 Ingress 创建正常 pending task。
3. Root 按[私有配置](../deploy/cloud-entrance-owner.json.example)与
   [装配片段](../deploy/cloud-entrance-owner.py.example)建立独立 loopback
   handler，复用 `make_http_handler`，既有443仅接 `/cloud/node`。
   保留当前 Host/XFP/CA/无redirect/无upstream retry 边界，原
   `mcp_port/oauth_port`/Chat插件不变。不把入口再塞进旧 receiver 的
   task-bound runtime。配置/文件0600、目录0700，全部留私有。
   Cloud侧车源码使用本叶及其c46/已审22b纯库依赖，复用现有隔离依赖环境；
   现生产69cde80尚无source参数，不能只往旧包追加新入口单文件。
   不构造现代API/Store，不替换原Chat OAuth/MCP实例或宿主原核心。
4. 用纯 `identity(policy)` 得到稳定 entrance/node ID，填[grant模板](../deploy/cloud-entrance-grant.json.example)，
   再显式 `entrance.install(grant, token_sha256=..., authorized=True)`。
   `bootstrap(grant_id)` 仅返回非秘密身份、callback、scope、expiry及
   request format；将其交给本人原 published 环境/已运行任务。
   网络 secret 使用目的地限定 placeholder，不能复制本机 auth/home。

安装/hello 不需要原 Mesh task，也不要求 Leader 正在执行。Mesh authority
正常可达时，Leader 空闲/尚无worker claim同样可以入队。authority 本身
不可达时，新提交保留原 unknown，不据连接错误换ID重发。后续实际执行
由原 Worker/authority 领取；问答/steering仍核原task/epoch/interaction，
原approval和secret问题不能被普通入口文字批准。

## 原身份、未知和连接内双向

CloudHTTPClient 无需修改；发送的新 envelope 为：

```json
{
  "format": "mesh-cloud-entrance-request/1",
  "entrance_id": "FROM_NON_SECRET_BOOTSTRAP",
  "node_id": "FROM_NON_SECRET_BOOTSTRAP",
  "request_id": "RETAIN_ORIGINAL_OPERATION_ID",
  "action": "hello",
  "arguments": {}
}
```

客户端使用 `CloudHTTPClient(bootstrap['callback_url'], root_injected_placeholder)`
发一次原请求；placeholder/对应非秘密变量名由Root按现有Cloud配置提供，
不猜注入、不显示凭据。缺bootstrap/授权/联网时报告具体缺项，保留本人
原Cloud任务，不创建环境或另发job。

仅允许 hello、入口 status、task_submit/status/result、原四项
conversation 操作及明确写权限的 conversation_sync。task_submit只收input，source/subject/project由安装
固定；status/result 收原 `entry_request_id`。result 只读原
IngressResults owner-approved publication，不返回 raw native result。
open/receive/reply/receipt 沿用原 conversation/request/message/reply/cursor
合同：open 可在 task pending 时发生，不造一个 task 来满足它。

所有 Mesh 写调用先保存不可变 intent；同ID变内容冲突，unknown 只观察
原status/receipt，重启/换模型/新授权都不再POST。新credential的明确
owner grant与操作账本分开：授权不会清理原request或unknown；撤销原
grant不被重复install复活。每个入口最多4096个原写操作，满后显式gate，
不删除旧意图。hello及其ACK在本地同一事务完成，不是模型执行证明。
read/等待返回前重新核TTL/撤销；write已准入后保留原回执，即使返回前
授权失效，也仅由新的明确owner授权查询，不重放提交。

receive在既有连接内发布实际进展/原问题/批准结果，reply仅将新记录的
原消息交给原bridge，receipt只查询。没有后台loop、主动唤起或客户端
关闭后的模型响应承诺；`background_wake_supported=false`。消息/完整
批准结果留原会话/IngressResults，新入口不冒称成果经过独立执行验收。

若回复在原task尚未claim时到达，沿原bridge保存并返回
`awaiting_original_live_task`，不能说Leader已收到。Cloud客户端可在连接
内显式调用 `conversation_sync`，要求 `tasks:submit`，参数严格为原
`conversation_id/request_id/message_id`，不重传或更换原消息内容：

```json
{
  "action": "conversation_sync",
  "request_id": "RETAIN_THIS_SYNC_CONTROL_OPERATION_ID",
  "arguments": {
    "conversation_id": "ORIGINAL_CONVERSATION_ID",
    "request_id": "ORIGINAL_INGRESS_ENTRY_REQUEST_ID",
    "message_id": "ORIGINAL_RECORDED_CLIENT_MESSAGE_ID"
  }
}
```

以上为同一新envelope内的action/request_id/arguments片段；format与固定
entrance/node ID仍必需。尚无claim则明确awaiting/post_attempted=false；
客户端观察正常task状态后，可用下一次显式sync控制操作ID推进，三个
原参数ID始终不变。同一个控制操作ID只返回已保存回执，不用它偷偷重试。
这不要求Root日常手工step，也不创建claim、task、keeper或后台定时器。

共享 `conversation_sync.sync_original_message` 自己验证写权和原to_mesh
消息，复用bridge原intent/receipt。已有native attempt时仅查原receipt；
未准入的原消息只能在显式写sync中首次尝试，明确waiting后可再次推进。sync进入bridge前也先
记录私有intent：若中断留下unknown，即使缺native receipt也不再POST，
返回 `sync_delivery_unknown`。只有明确awaiting/no-post结果可供之后推进。
read/receive/receipt始终不drain；旧MCP七工具及默认Chat source均未改。

## 最小检查与真实剩余

隔离小组使用真实SQLite/Ingress、原Store问题/steering事务及一次实际
loopback HTTP。覆盖零task/零node时hello与submit、固定身份/只读scope、
原提交丢ACK后重启/换授权只观察、TTL/撤销/等待后不泄露、原问题回答/
native ACK丢失、无live claim时批准结果读取。不用账号、模型或provider。
原7项本地检查0.602s、0skip通过。随后仅新增1项sync集成检查（0.042s、
0skip）：pending回复→自然真实claim→客户端sync→原Worker ACK，覆盖
只读不drain、重复sync/丢ACK/重开不二投及bridge前中断缺receipt不重投。
没有重跑原7项；这些不是公网/主账号/宿主或官方Cloud证据。
新模块/装配片段也通过Python3.6 grammar检查，未称宿主实跑；未重跑
此前已通过的现代SDK小组。旧CloudJobs/Membership/runtime等文件零修改。

Root 仍需审查源码，受控安装专属私有peer/source allowlist与同443窄路由，
显式owner授权及原Cloud环境的真实bootstrap/secret注入，然后在同一已
运行任务验证hello、空闲入队、连接内问答和原receipt。IP目的地/secret
可用性按真实官方环境验证，不假定已支持。长期Goal保持active。
