# Owner-bound ingress foundation

PAM-006 的第一片是可选的本地 authority 业务合同，不是已安装的
ChatGPT plugin、OAuth server、Cloud job 或新 Leader。实现位于
`assistant_mesh/ingress.py`，复用原 Store task 与连续 native session；
不发放 operator/worker 权限，不拦截原生 Shell、网络、文件或 MCP。

## 身份和部署配置

启用入口时，由 owner 的私有 server 配置确定本人 namespace、所需
任务 capabilities 与原生 session scope。例如以下都是虚构配置值：

```json
{
  "node_id": "authority-fixture",
  "ingress": {
    "owner_id": "owner-fixture",
    "required": ["leader"],
    "session_scope": "leader:owner"
  },
  "peers": [{
    "role": "ingress",
    "token_file": "/PRIVATE/ingress-peer-token",
    "ingress": {
      "owner_id": "owner-fixture",
      "source": "fixture-chat",
      "subject": "verified-subject-fixture",
      "scopes": ["tasks:submit", "tasks:read"]
    }
  }]
}
```

`required` 与 `session_scope` 可省略，默认是 `['leader']` 与
`leader:owner`。它们只能由部署配置决定，不能由入口请求覆盖。
`validate_config(config)` 和 `validate_peer(peer, owner_id)` 是纯校验；
server 在创建 Store/入口表之前检查配置。peer 外层只允许
`role/token_file/ingress`，内层只允许示例里的四项；node、capabilities、
未知/重复 scopes、错误 owner 均拒绝。空 scopes 是没有读写权限的配置。
module 不读取 token 文件，也不将其路径传给模型或返回给入口。
server 启动时读取部署凭据，涉及 ingress 的重复 bearer 一律在打开
业务账本前拒绝；不能靠 peers 顺序把 scoped 入口映射成 operator 或
另一个 subject。

subject/source 来自**已经鉴权的部署 peer**，不是请求、prompt、email
字符串或模型自报。未来 OAuth sidecar 必须先核对 issuer、audience、
expiry、PKCE、scopes、revocation 和 owner 映射，再选专用可撤销的
ingress peer；本模块不代替这些检查。不向 Chat/Cloud 传 operator bearer，
也不把公共通用 `shell(command)` 当作身份接入。

## 提交和状态

程序接口为 `Ingress(store, authority_id, config)`，以及
`submit(authenticated_peer, payload)` / `status(authenticated_peer, payload)`。
server 的入口 routes 是：

| Route | Scope | 唯一允许的 payload |
| --- | --- | --- |
| `POST /v1/ingress/tasks` | `tasks:submit` | `request_id`, `input`；可选 `project_id`, `agent_id` |
| `POST /v1/ingress/task/status` | `tasks:read` | `request_id` |

入口 role 先分流，不访问已有全局 status/discovery/operator routes；
其他角色也不能借新 route 转换身份。请求没有 global task ID、parent、
required、context、native thread/session、预算或凭据参数。

任务描述保持开放，不固定领域或平台。`project_id` 用于项目分组；
`agent_id` 是给 Leader 的请求标签，保存在
`context.ingress.requested_agent_id`，不是当前 native agent/session 选择器。
原 Store 明确 `/plan ...`、`/goal ...` 的文本语义保留，但部署 session
scope 不变；入口不会自行创建、替换或压缩 native thread。

`request_id` 必须由调用方在首次提交前保留。authority/owner/source/
subject/request ID 的精确字节确定完整 SHA-256 namespace，实际 task ID
为 `ingress-<64 hex>`。同一 authority transaction 内：

1. 查原 source/subject/request 绑定和 canonical 内容 fingerprint。
2. 新请求复用 `Store._create_task_in_db` 创建普通 task，再记录绑定。
3. 同 ID/同内容重试返回原 task；内容/项目/agent 变更冲突。

创建 task 或写绑定任一步失败都回滚。两个入口实例同时收到重复请求
也只能产生一个 task。已存在派生 task 但缺少绑定不能被入口认领；
缺少 task 的旧绑定不能自行重建。未知效果、needs_review、版本/模型/
节点变化都不许可重新执行或换 ID。连接/token 轮换要保留原 source/
subject；改变它们会形成不同身份，不是旧任务恢复协议。

配置 authority/owner namespace 持久绑定，不能默默换 owner 认领账本。
调整 owner 部署任务策略只影响新请求，旧请求 retry 保留原 task 和
session policy。namespace 迁移、身份合并需单独设计，不自动执行。

响应只含 request/task receipt、当前 status、`result_available` 等有界
元数据；`accepted` 只证明收账，`execution_verified` 始终 false。
未知未来 status 显示 `unrecognized`，不反射任意数据库文本。
不返回原输入、raw result、checkpoint、native thread、账号、机器标识
或整个私有账本。没有全局 task ID 查询 fallback；跨 source/subject 和
不存在的 request 都以同一 PermissionError（HTTP 403）拒绝。
readonly peer 只能读同一绑定；submit-only retry 的有限收账 receipt
不是通用读权限。

原生终端也可复用该独立入口，输入放在仓库外本人 0600 JSON：

```sh
python3 -m assistant_mesh --config /PRIVATE/ingress-client.json ingress-submit --payload-file /PRIVATE/request.json
python3 -m assistant_mesh --config /PRIVATE/ingress-client.json ingress-status --payload-file /PRIVATE/query.json
```

request JSON 使用上表的提交字段，query JSON 仅有原 request_id。
client 仅带受限入口 control_url/token_file，不是 operator/worker 配置；
CLI 不接收身份、任务 epoch、权限或 native session 覆写参数。

## 当前证据和未完成部分

31 项本地合同测试覆盖原子失败回滚、双实例并发重试、重启保留、
原生 scope、subject 隔离、权限/类型/UTF-8 边界、派生 ID 不认领旧任务、
unknown 不重放和私有状态不外泄。它们是局部 fixture，**不是**本人
ChatGPT 账号、真实 OAuth、临时 Cloud 节点或跨设备执行的验收。

已增加独立 [受限成果合同](INGRESS-RESULTS.md)：operator 显式审查后发布
有界摘要/公开引用，入口以 `POST /v1/ingress/task/result` 和私有 payload
`ingress-result` 回查；operator 使用 `/v1/ingress-result/publish` 或
`ingress-publish`。它不自动暴露原始结果，也不独立验证引用内容。

当前不提供 steering/control、结果全文或 artifact 下载、worker enrollment、
Cloud 提交/恢复、MCP schema/transport、HTTPS/OAuth 和定时入口。
安全成果发布、按任务受限的临时节点身份、owner 主账号真正接入仍由
后续片完成；不能将 `result_available=true` 当作入口已经能读取成果。
基础研究与完整实际账号验收继续见 [ENTRYPOINTS-RESEARCH.md](ENTRYPOINTS-RESEARCH.md)。
