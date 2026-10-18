# 本人启动的官方 Cloud → Mesh

PAM-006e，作者 `/root/cloud_outbound_handoff`；基线 `0aec228`，独立分支
`work/cloud-outbound-handoff-20261018`。旧 `cloud-current-surface` / `d1105fe`
和主协调文档未改。没有生产/GUI/账号操作、Cloud submit、新模型 key 或费用。

结论：本人继续原 published 环境/已启动任务，由 Cloud 自己连公网 Mesh。
这不要求 Mesh 先安排或发起 Cloud job。本片补上独立 adopted 账本与 Cloud
身份双向会话；原发起作业账本、旧 unknown 和一次发送边界全部保留。

## 最小 source diff

- `CloudAdoptions` 没有 CLI、auth home、prompt、submit 或 provider 创建接口。
  `adopt(binding, authorized=True)` 只记录 Mesh 侧的 owner enrollment work；
  它可发生在本人 Cloud 任务已经启动之后。新私有 `cloud-adoptions.sqlite`
  与原 `cloud-jobs.sqlite` 分开，不把已启动任务补写成 Mesh 曾发送。
- `CloudMembership(CloudAdoptions(...))` 复用原 `mesh-enrollment/1`、
  `mesh-cloud-membership/1`、`mesh-cloud-request/1` 与 CloudHTTPClient。
  可以先准备短期节点 grant 并真实 hello，状态明确为
  `owner_authorized_unattested_entrance`、`provider_identity_verified=false`。
  原 CloudJobs 分支仍要求 `submitted`；没有放宽旧 unknown。
- 独立正常官方 UI/本人原任务观察，经部署级 `provider_verifier` 严格 True
  后，`record_provider_observation` 才追加已有 task/env/证据引用，标为
  `owner_observed_official_cloud`。没有新 HTTP 准入字段，也不接受 VM 自报
  URL/token/OAuth 即证明物理来源。它从来不宣称 `execution_verified=true`。
- 新 `conversation_open/receive/reply/receipt` 复用已有耐久会话与真正的
  question/steering bridge，固定 source=`openai-codex-cloud`、subject=原 node。
  Source 只由 owner 安装代码选；默认 Chat 仍 `chatgpt-chat`，旧 Chat journal
  不能被换 source 重开。MCP OAuth app 也不能接 Cloud source 冒充 Chat。

`mesh_task_id/epoch/lease_id` 是 Mesh 侧 enrollment work/grant 的关联，不是
“Mesh 曾发起 provider 任务”的证据；加入工作可在本人 Cloud 启动后建立。
hello/保存成果不要求先有 provider attestation。业务能力调用与 task 写入
仍需真实 live authority，批准的 conversation question/steering 仍核原
task/epoch/interaction。不能用占位字段或常量 True 伪造这些权限。

## Root 需要部署的内容

1. 在现有 VPS 公信 HTTPS:443 后绑定一个私有 loopback Cloud handler，
   设置既有 TLS 配置的 `cloud_port`；只转发 `/cloud/node`。保持 Host、
   覆盖 `X-Forwarded-Proto: https`，复用现有证书/续期，不公开管理 API。
2. owner factory 使用 `CloudAdoptions(private_dir, owner_binding, ... )`，
   为本次 owner enrollment 保存原 request/node/continuation 和短期 grant，
   交 `membership.prepare(..., token_sha256=..., authorized=True)`。这是
   Mesh 自己的受限 bootstrap，不是新模型 API key；已有可用授权凭据优先
   复用，不复制本机 OAuth/auth.json/CODEX_HOME/operator bearer。
3. 给 Cloud 可读的**非秘密** `membership.bootstrap(job_id)` 与注入键名；
   bearer 用正常 Network secrets/既有可信注入，精确允许公网主机。
   CloudHTTPClient 保留标准 proxy/CA，HTTPS443/拒 redirect/一次调用已具备。
   IP 能否用作当前 Cloud secret 目的地、已有任务能否取得新增配置都须真验；
   不能从 VPS IP 证书或旧配置推定。若原环境缺配置，更新原环境并 republish，
   不重建环境；已发任务保留，不因补配置再 POST 同一工作。
4. 同一 facade 给 Cloud 单独的 ConversationService/private directory，
   backend 固定 Cloud source；ConversationBridge 固定该 node principal。
   现有已打开的 `api.ingress` / `api.ingress_results` 可直接复用：

```python
backend = LocalIngressBackend(api.ingress, api.ingress_results, source=DISPLAY_ID)
service = ConversationService(cloud_conversation_dir, owner, backend, source=DISPLAY_ID)
authority = ConversationAuthority(api.ingress, sources=(DISPLAY_ID,))
# fixed_client.request('/v1/ingress/conversation', body) 仅在 owner 私有代码中
# 调 authority.dispatch(fixed_cloud_peer, body)，不增加公开 /v1 路由。
bridge = ConversationBridge(service, fixed_cloud_principal, fixed_client)
membership = CloudMembership(adopted_jobs, ingress=api.ingress,
    conversation_service=service, lease_validator=trusted_live_authority)
Handler = make_http_handler(membership, public_url, trusted_proxy_peers=('127.0.0.1',))
```

上例是部署 hook，参数来自 Root 私有安装；不新构造/迁移 Store，不是可让
客户端填授权布尔的工具。最低 hello/完整成果试验无需开放 capability call。
连接后以原 `task_submit`/现有 ingress 建立真正的 Mesh 工作再 open 会话；
接问答时使用原 request/message/reply/cursor。read/receive 不偷偷提交任务；
回复 ACK 未知只查原 receipt，不重投 native steering。

5. provider 来源可并行或稍后核：复用本人已确认 GIH ggiy / @163 绑定，
   正常重开本人已存 Cloud 任务，核原环境/实际 Cloud 执行位置/准确持久 ID。
   保存独立私有观察再由 verifier 提升 provenance；不重开身份菜单、不猜
   `client-new-thread`、不把普通 Chat/setup 或内部 Alibaba `cloud` 当官方 VM。

## 本人下一句（原已发布环境/同一已存任务）

> 请从当前 personal-assistant-mesh Cloud 环境主动连接公网 Mesh。读取
> AGENTS.md 和入口合同，复用现有 CloudHTTPClient、Root 安装的非秘密
> bootstrap 和受限 Mesh secret placeholder，不显示凭据。用原加入
> request/node/continuation 发一次 hello；如无 bootstrap/注入/网络权限，
> 只报告具体缺项并保留本任务。不要创建环境、提交另一 Cloud job 或复制
> 本机认证。加入后按原 Mesh request 打开会话，接收进展和一个实际问题，
> 用同 message/reply/cursor 回答并查询原收件回执，再回传一份无秘密完整
> artifact/report。分别报告 owner 授权接入、官方 provider 来源核验和
> 真实执行/完整成果到哪一步；未知只查原记录，不换 ID 重发。

若本人正开始新任务，正常 web/app **Work in → Cloud** 选择原 published
环境；手机 **Codex → 原环境**。现有任务直接继续，不要求再发送一个 job。
官方步骤见 [OpenAI Docs：Cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environments#start-a-task-in-codex-cloud)。

## 证据与实际剩余

| 层 | 可据什么认定 | 本片结果 |
| --- | --- | --- |
| 模型在官方 Cloud 运行 | 正常官方任务的 Cloud 环境及真实 native tool 调用/输出 | 没有目标 Cloud 模型实跑 |
| 官方 job/provenance | 独立正常 UI 核本人已存任务/env，trusted verifier 追加观察 | 仅源码/合成 verifier；未核真实 job |
| Mesh owner enrollment | 原受限 credential 的实际 hello/持久记录，provenance 单独标级 | 实际隔离 loopback/SQLite通过，未生产连接 |
| 双向与完整成果 | 原问题/原回复到真实 Mesh 事务、游标/receipt、完整原字节读回 | 实际隔离事务/传输通过，未本人验收 |

107 项针对性检查 7.476s、0skip，另 10 项原 Chat 授权/业务检查 0.024s 通过：
新 adopted 路径实际 HTTP 完整
153600B artifact/report、过期后离线读回；旧 launch unknown 不变；source/
owner/业务权限边界；真实 SQLite 问题回答/steering、ACK 丢失只查原回执、
重复 message 不再投递、receive 后过期不泄露。第一轮新 loopback fixture
错误使用宿主代理获 502；仅 fixture 改成独立直连 loopback，正式客户端
代理逻辑未改。本机未安装现代 MCP SDK，本片没有跑其 ASGI 组；Root 集成
可用其既有隔离 SDK 环境核这一小组。不新增依赖/扩大完整测试。以上不是
公网、真实官方任务或生产模型的证据。

本片按 OpenAI Docs 先 search 后 fetch 官方环境页，确认 network secret
为 HTTPS443 目的地限定 placeholder、电脑休眠后任务可继续、发布更新
与已有任务保存状态有区别。文档未提供本方案可直接调用的个人 Cloud job
签名证明接口；不能假定本人拥有需申请的 Enterprise OIDC。
[官方环境/联网/Secrets 说明](https://learn.chatgpt.com/docs/environments/cloud-environments)

Root 下一只需合并小片、安装实际 facade/私有 bootstrap/会话 bridge 并接
既有窄路由，然后本人同任务发上述验证。不调用 Cloud submit；连接内
反向消息已允许，客户端关闭后的主动唤醒仍延后。长期 Goal 保持 active。
