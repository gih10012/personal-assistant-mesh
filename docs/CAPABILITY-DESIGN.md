# 资源、算力、性能与可达性都可以成为能力

这个层回答“谁声称能做什么、当前有什么证据、如何找到路径、是否有权跨主体使用”。它不规定 AI 只能做哪些事，也不把面板按钮或预先安装的插件当成能力边界。Codex / Pi 可以继续使用已授权的原生终端、创建工具、发现接口，并把实际产出的新工具登记为可复用能力。

设备是能力的承载者，不是不可变角色。公网入口、反向连接、LAN 代理、协议网关与模型推理都能用同一套开放描述表达；算力和性能不是固定机器等级，而是带工作负载、时间、来源和证据的观测。这里没有固定能力类型表、固定调度排名、自动网络扫描或底层设备控制。

## 四件必须分开的事

| 对象 | 回答的问题 | 不代表什么 |
| --- | --- | --- |
| Advertisement | 提供者声称能做什么、租约还有效吗？ | 不证明执行成功，也不给远端调用权限 |
| Observation | 在什么时间、什么工作负载下观测到什么？ | 一次成功不证明任意负载、任意时间都可用 |
| Graph edge | 提供者声称存在什么依赖、代理或可达路径？ | 路径可发现不等于路径上的每一跳获准使用 |
| Exact grant | 哪个主体可在期限内使用哪个能力的哪个动作、哪个范围？ | 不给底层设备、账号或整台主机的无限权限 |

注册与健康上报默认都是 `declared`。只有部署明确绑定的独立 `trusted_verifiers`，携带具体 evidence，且不是该能力提供者本人，才能写 `verified` 指标。指标的验证不会把整个 advertisement 自动升级为“全部已验证”。`available` 表示当前登记租约未失效、未撤销、未自报失败，不是注册中心已经真实运行了工具。

## 描述保持开放，性能保持具体

`kind`、`spec`、指标名、单位、图关系都不限定成枚举。例如同一终端可以登记 `llm.inference`、`terminal.native`、`network.reverse_relay`，也可以登记模型后来创造的类型。`spec` 可以携带输入输出 schema、工作负载限制、调用方式的非秘密描述、额外成本、提供者的数据范围、实现/版本与证据引用。

```json
{
  "id": "laptop-local-inference",
  "kind": "llm.inference",
  "spec": {
    "model": "provider-discovered-model",
    "workload": {"context_tokens": 8192, "quantization": "q4"},
    "inputs": {"type": "text"},
    "outputs": {"type": "text"},
    "extra_cost_minor": 0
  }
}
```

性能上报另存为指标，例如 `metric=tokens.generated.per_second`、`value=18.2`、`unit=tokens/s`、`sample_time=...`、`source=bounded local benchmark`，并在 evidence 中保存 method / workload / samples / result reference。`received` 是 authority 收到观测的时间；它不能替代实际采样时间。吞吐、首 token 延迟、可用 RAM、当前负载、路由时延、电池、能耗和可靠性都可用同一机制描述，不强行折算成一个“性能分”。

指标保留 capability epoch，更新实现后旧数据仍可查，但标为 `current_epoch=false`；模型不应把旧实现的测量当作当前实现的保证。注册中心不会根据指标自行安装模型、配对蓝牙、开放端口、发起网络探测或付费购买算力。

## 租约、版本与真实事务

`Registry` 复用已有 `Store.transaction()` 的 `BEGIN IMMEDIATE`、WAL 与 authority clock，不另建外部数据库。能力、指标、图关系、授权请求、授权和审计记录保存在同一 SQLite 文件中。写状态与写审计必须同事务成功或回滚。

首次 advertise 返回 `epoch=1`。相同内容的重复注册是幂等重试，不延长租约；续租使用明确的 `renew`。更改描述/实现或重新发布已经过期的能力，必须提交当前 `expected_epoch`，并递增 epoch。旧线程、旧 RPC 或旧部署不能悄悄覆盖新描述。续租、图连边和撤销同样检查 epoch。

撤销保留 tombstone，提供者不能靠重发同一个 ID 自动恢复被撤销的能力。后续若有新的明确授权，可以登记新能力 ID；目前没有隐式“解除撤销”入口。无心跳不能通过续旧租约伪装连续在线，必须带版本重新声明。能力更新或失效同时使依赖旧 epoch 的图边与远端 grant 不可用。

资源图支持任意关系与环路，不假装所有网络都是一棵树。边只能由 source capability 的提供者声明，不能替别人伪造其设备控制权。边的期限受 source、target 的租约共同约束，返回 `invocation_authorized=false`。路径可用于模型判断候选执行点，实际调用仍须逐跳检查权限和运行时状态。

## 远端权限不会由模型自行提升

actor 必须由服务端认证后的 peer 绑定：当前集成约定 owner operator 为固定 `operator`，节点为 `node:<configured-node-id>`。不能采用 HTTP body、prompt、capability spec 中的 actor / role / owner 字段。`Registry` 是服务端内部类，不承担 bearer token 验证；应用接线必须保持这条信任边界。

同 principal 的能力查询/使用不另加远端 grant，这不修改原生终端已有权限，也不扩大其权限。跨 principal 则先 `request_grant`，随后由 owner 的独立操作员入口明确 `grant`。节点可以请求授权，不能审批自己的请求，不能冒充 owner，也不能通过 `trusted_verifiers` 的名字取得部署配置中的验证身份。

grant 绑定 exact principal、capability ID、capability epoch、action、canonical JSON scope 和期限。scope 的对象 key 顺序不影响匹配，其余内容必须完全相同；字符串 `*` 没有通配符语义。读和写是不同 action，单设备、单路径或单结果范围不能扩成整机或整个局域网。重试 grant 不延长期限，不恢复已经撤销或过期的授权；需新 request ID 和新审批。

`authorize` 每次在真实事务中重新检查能力、epoch、grant 期限与撤销状态，即使提交同一个 request ID 也不能拿之前的 `allowed=true` 绕过撤销。它只是一项授权检查，不执行任何动作、不是永久 bearer permit、也不承诺远程副作用幂等。执行网关将来必须在开始实际操作时重新验证，并对执行进行独立租约/幂等/不确定结果记录；目前本模块没有 remote invocation 实现，不应把授权成功展示成“远端工具已执行”。

## 秘密和不可信资料

描述是给已认证主体发现能力用的非秘密资料，不是凭据仓库。模块在存储 metadata 前移除明确 credential 字段，并去掉 URL userinfo / query / fragment。验证 evidence、性能值和 source 也经过同样处理。私密认证绑定留在提供节点的受保护文件中；不得把 auth.json、token 内容、配对密钥写进 registry、GitHub 或面板。

自由文本无法可靠判定是不是秘密：调用者仍不能把凭据嵌进 description、reason、普通字符串、URL path 或改名字段。自动去除 credential 字段是额外保护，不是全能 DLP 保证。capability 描述、图关系和证据都是资料，不是指令或新增授权；模型阅读它们不能被要求越过 owner 当前授权、隐私或默认零新增支出边界。

## 接口

```python
registry = Registry(store, owner_principal='operator', trusted_verifiers=())

advertise(actor, id, kind, spec=None, description='', lease_seconds=90,
          principal=None, expected_epoch=None)
discover(kind=None, principal=None, include_unavailable=False, limit=100)
describe(id, include_unavailable=True)
renew(actor, id, epoch, lease_seconds=90, health=None)
observe(actor, id, metric, value, unit, source, sample_time=None,
        evidence=None, verification='declared', epoch=None, observation_id=None)
link(actor, id, source, target, relation='depends_on', spec=None,
     lease_seconds=90, epoch=None)
graph(include_unavailable=False, limit=200)
revoke(actor, id, epoch, reason='')
request_grant(actor, id, action, scope, request_id=None, reason='')
grant(owner, request_id, expires_seconds=300, grant_id=None)
authorize(actor, id, action, scope, grant_id=None, request_id=None)
revoke_grant(owner, grant_id, reason='')
audit(after=0, limit=100, principal=None)
```

`discover` 返回 capabilities 与 `as_of`；`describe` 增加最多 100 条时间排序的指标；`graph` 返回 capabilities / edges。结果目前是有上限的 discovery snapshot，不是无限规模图引擎。稳定 observation ID 的重试还应保持同一个 `sample_time`；改变任何测量事实都需新 ID。审计只有追加入口，部署数据库管理员仍持有修改文件的权限，它不是多副本共识或抗管理员篡改的证明。

### HTTP、native tools 与连续会话 CLI

服务端接线从认证 peer 确定 actor。`resources.trusted_verifiers` 只可在受保护的 server 配置中声明；请求 JSON 无法添加 verifier、改变 owner 或改写 caller principal。

- `GET /v1/resources`：metadata discovery，支持 `kind` / `principal` 搜索筛选、`include_unavailable=1` 和 `limit`。
- `GET /v1/resource?id=...`：只读完整 descriptor 与 metrics，默认允许查看过期或撤销历史。
- `GET /v1/resource/graph`：只读图 snapshot。
- `GET /v1/capability-events`：默认最近 100 条审计；显式 `after=0` 可从第一条开始回填。返回 `page.mode`、`after`、`limit`、`next_cursor`、`has_more`，避免页面永久只看最早事件。
- `POST /v1/resource/action`：`{"action":"advertise","arguments":{"id":"...","kind":"..."}}`。worker/operator 可以使用已实现的方法；只有 operator 可以 grant / revoke_grant，viewer 所有 POST 资源动作均拒绝，即使请求的是 discover。

上述读接口允许已认证 viewer/operator/node 观察本人的非秘密能力资料。这里的节点仍属于当前单 owner 部署，不是开放给陌生组织的多租户目录。`principal` 在 discover/audit 的查询中只是过滤条件，不能变成调用身份。HTTP 的固定方法集合保护协议实现入口，不是限制 native 终端能做什么。

新的 native Codex thread 增加通用 `mesh_resource(action, arguments)`，只按需查 descriptor。已有线程按其 original rollout 恢复，不用新 thread 换取新工具，也不向该版本 `thread/resume` 塞不支持的 dynamicTools 字段。官方说明 dynamicTools 保存在 rollout 元数据并在恢复时取回；本实现保留这条原生连续性。[Codex App Server 文档](https://learn.chatgpt.com/docs/app-server#start-or-resume-a-thread) Worker 在每项任务的参考资料里给出简短能力概述及私有配置文件的 CLI handle；它不是 auth/token 值，也不做应用自写记忆摘要。

CLI 可在 mesh 安装目录执行，或按参考资料中的 `cwd` 与 `argv` 使用：

```sh
python -m assistant_mesh.cli --config /private/laptop-worker.json resources
python -m assistant_mesh.cli --config /private/laptop-worker.json resource --action describe --payload-file /private/describe.json
python -m assistant_mesh.cli --config /private/laptop-worker.json capability-events --after 0 --limit 100
```

payload 文件可以是 arguments object，例如 `{"id":"capability-id"}`；也可包含完整 `action` / `arguments` object。文件必须本人所有、regular、非 symlink、权限无 group/world bits，并置于 git worktree 之外。配置和 token 同样从受保护文件读取，不接受 argv 中的 token 值或 actor 参数。`--principal` 仅能用于 discovery，不是提升身份的办法。旧 Codex 及 Pi 会话都可通过原生终端使用这条路径；Pi 扩展的专用资源 tool 尚未据此声称接线。

## 已验证与尚未验证

`tests/test_resources.py` 使用临时真实 SQLite 数据库，验证开放 schema、重启持久性、租约/健康、epoch fence、撤销、观测来源、独立验证、credential 字段清理、图路径、exact scope / principal / action、授权到期与撤销、稳定请求冲突、审计分页、事务异常整体回滚，以及两个并发更新只有一个能通过同一 epoch。

`tests/test_resource_api.py` 验证 server 绑定身份、caller forgery、viewer 只读、租约和撤销后的拒绝、固定 owner 审批、部署 verifier 与最新事件分页。`tests/test_resource_cli.py` 启动临时真实 loopback HTTP 服务，验证 CLI 私有 JSON 文件、401/403/409 边界、worker 工具 task lease gate、旧 native thread 保持 resume，以及新 thread 的资源 tool 注册。它们不会启动模型、借用用户 token 或操纵真实设备。

这些测试不证明任何 GPU、手机、蓝牙或网关已经可用，也不证明真实部署、模型实际调用、面板或远端执行已经端到端验收。模块没有新增支出，没有自动扫描或设备副作用。今后的验收应把“AI 创建了什么工具、验证了哪些终端能力、在何种负载下有什么测量”放在重点；面板只把这些真实状态展示出来。
