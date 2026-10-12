# 官方 Codex Cloud：临时节点与受限任务入口

实现位于 [cloud_membership.py](../assistant_mesh/cloud_membership.py)，复用
[CloudJobs](CODEX-CLOUD-JOBS.md) 的持久提交 intent。这里的显示身份固定为
`openai-codex-cloud`；原有内部节点 `cloud` 仍是阿里云 VPS，不能混用。

本片完成的是合同、私有 journal、完整成果回调和受限入口代码，不等于
官方 Cloud 已入网。单元测试的 Cloud CLI 是 fake；真实本机 HTTP 测试也
不是从官方 Cloud VM 发出的请求。实际官方 job、HTTPS 可达、凭据注入与
能力往返须由原 owner 授权的集成验收另行记录。

## 原则与官方接口

Cloud 是有独立原生工具的临时执行节点，也可发布和续接自己来源的 Mesh
任务；不替换连续的 Codex Leader。Mesh 只增加受管入口，不拦截原生
Shell、文件、网络、模型或 MCP，不强迫 VM 安装本机整套环境。

最新官方说明区分 published environment 与每个任务的隔离 VM。本地
已登录账号、skills、文件和 secrets 不会自动出现在 Cloud。网络 `All`
或域名配置不能代替 Mesh 身份授权；默认保存的 VM 可恢复七天，Mesh
任务、成果和续接身份不能依赖这段保存期。
[官方 Cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environments)。

2026-10-12 本机核对的官方 CLI 为 `0.162.1`。`cloud exec --env ...
--branch ... --attempts 1 -` 支持 stdin prompt；`list --json` 返回
`tasks` 与 `cursor`，任务字段包括真实 `id`、`url`、`status` 和可能为空的
`environment_id`。只有同 provider ID 与指定环境的观察可供该 adapter
绑定，不能按标题、nonce 或“最近的一个任务”认领。
[官方 CLI reference](https://learn.chatgpt.com/docs/cli/reference)，
[固定版本参数](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/cloud-tasks/src/cli.rs)，
[固定版本实现](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/cloud-tasks/src/lib.rs)。

实际只读 `cloud list --json --limit 1` 返回 exit 0、stderr 空、一个任务
与 cursor，但该项 `environment_id=null`。这只是账号 CLI 的一次只读
观察，不能证明本项目的 published environment、Cloud 入网或新旧环境
兼容。没有通过本片提交 job、创建环境、修改账号或新增付费资源。

## 身份和持久状态

一个 CloudJob 固定原 `mesh_task_id`、`mesh_epoch`、`request_id`、官方
environment、branch 和完整 prompt。`planned_id(task_id, request_id)`
可在保存 intent 前纯计算 job ID，随后用 `node_id(job_id)` 得到稳定
临时节点身份；二者均不预留执行权、不提交任务、不证明 provider 回执。

单独的 `mesh-cloud-membership/1` grant 固定同 task/epoch、原 `lease_id`、
短期过期时间、可执行 scope、具体能力 ID 与回调 URL；嵌套
`mesh-enrollment/1` 共用新设备合同。临时 `runtime.state_ref` 指向 Mesh
里的持久状态，不指望 VM 的磁盘永远存在。所有 `*_ref` 是私有 opaque
部署引用，不是要求所有系统使用相同路径，更不能放实际凭据值。
完整跨环境合同见 [新设备入网](ENVIRONMENT-ADAPTIVE-ENROLLMENT.md)。

保存位置是既有 CloudJobs 私有目录中的同一 SQLite journal 和
`node-artifacts/`。Linux 参考要求目录 0700、常规单链接文件 0600，拒绝
不安全权限和符号链接；其他平台应实现自己的私有存储/ACL 适配，协议
并不要求 Windows、手机或 Cloud 都运行 systemd/Python 常驻服务。

凭据是独立、短期、task-bound 的节点 bearer。`prepare()` 只保存其
SHA-256，不生成、复制或注入 secret。实际值由已授权的 Cloud secret
注入或私有适配器提供；不得复制 `CODEX_HOME`、`auth.json`、本机 skills
目录、完整 operator bearer 或账号 OAuth token 到 VM/prompt/公开仓库。
`bootstrap()` 返回身份合同、回调 URL、scope 和 credential_ref，不含值。

## Trusted owner 集成顺序

以下接口不是可让模型自行填写 `authorized=True` 的公开 HTTP API。
执行前核对 owner、当前 authority、原任务/epoch/lease、已有 Cloud 额度和
已发布环境；真实账户、凭据和回调部署由 trusted owner 层处理。

1. 用原 task/request 计算 `jobs.planned_id(...)` 和 `node_id(...)`，构建
   固定原始 prompt：允许节点适应 VM，使用注入引用取得非 secret bootstrap
   和独立节点凭据；不要要求把整个本机登录环境搬过去。
2. `jobs.intent(payload)` 保存完整不可变任务。在一次启动前调用
   `membership.prepare(job_id, grant, token_sha256=..., authorized=True)`。
3. 选择唯一提交方式：兼容 CLI 时 `jobs.submit(..., authorized=True)`；
   已核实的官方网页入口则先调用 `begin_ui_submission(...)`，只有返回
   `submission_started=True` 才允许一次网页发送。不能两种都提交。
4. 网页路径需在原账号、环境、会话独立核实该次发送的确切回执，然后
   `record_ui_receipt(job_id, canonical_url, owner_confirmed=True)`。同 URL/env
   的实证状态可用 `record_ui_observation(...)` 保存；不是由模型猜测状态。
5. Cloud 节点用独立凭据发 `hello`。只有已绑定 provider 回执 + 正确节点
   凭据 + 同 task/epoch/lease 的实际 hello 才返回 `cloud_node_enrolled=True`。
6. 任务可调用有限 Mesh 能力、提交自己的任务、回传完整 artifact 和结果。
   VM 退出/租约过期后 owner 仍可从 journal 读完整成果，不能依赖 VM 恢复。

CLI/网页启动边界均先持久提交再发送；崩溃、超时、丢回执不能 reset、
换 ID 或重新提交。网页回执只接受事先记录的网页 intent，不能拿它覆盖
一次 CLI unknown。CLI 的人工 reconciliation 也不能冒充网页提交。
`submission_attempted` 表示一次边界已消耗；`cli_attempted` 仅表示 CLI，
`submission_channel` 明确区分 `cli` 与 `official_ui`。

## Authority facade 接口

```python
from assistant_mesh.cloud_membership import CloudMembership

membership = CloudMembership(
    jobs,
    ingress=existing_owner_ingress,        # 可选：复用原 Store/Leader 范围
    capability_gateway=managed_gateway,  # 可选：不能是任意 operator RPC
    lease_validator=live_task_authority,  # 执行/入口写操作必须配置
)
```

`live_task_authority(binding)` 接收原 grant 加 `job_id/node_id`，应核对当前
Mesh task/epoch/lease，返回严格 `True` 才允许新写操作。`prepare()` 与
每次新写入 intent 调用同一合同。旧 request 的已保存回执只供观察，不
代表获得新的执行许可。节点自己的过期时间与 owner 撤销仍始终检查。

`managed_gateway(binding, action, arguments)` 负责接到现有 thin capability
入口，按 grant 的具体 capability 集合过滤 discovery，并再次验证当前
task authority、能力 provider 权限和操作 receipt；不能给 Cloud 透传
全 operator API。无 gateway/Ingress/lease validator 时，相关操作明确
返回 not_configured gate，不能偷偷用全权限 bearer 补位。

`handle(token, envelope)` 是 HTTP/MCP 运输适配器调用的库入口。本模块
不打开 listener、不创建 Store、不部署 HTTPS/OAuth。运输适配器必须
验证 TLS、限制 body 大小、拒绝重复 JSON key/非法 JSON、从 Authorization
header 提取节点 token，并把错误映射成无 token/prompt 的固定类别；
不要把 role、owner_confirmed、lease_validator 或 operator 身份交给 JSON。

请求固定为 `mesh-cloud-request/1`，包含 job/task/epoch/lease、稳定
`request_id`、action 与 arguments，示例见
[request example](../deploy/cloud-membership-request.json.example)。

| Action | 参数与作用 |
| --- | --- |
| `hello` | `{}`；记录正确身份的实际握手，不声称独立执行证明 |
| `status` | `{}`；只返回该 job/membership 与已保存 artifact 元数据 |
| `artifact` | name/base64/bytes/SHA-256/media_type；完整字节不可变保存 |
| `report` | completed/failed、完整 result、已保存 artifact SHA 列表；provider 自报 |
| `task_submit` | 仅 input；source/node/owner/project/native Leader 范围由配置固定 |
| `task_status` | 仅该节点创建的 entry_request_id；不能遍历其他来源任务 |
| `task_continue` | 自己的 entry_request_id + 原 epoch/result SHA + 新 instruction；原 Store CAS/native history |
| `discover` | `{}`；只读已授予能力，由 owner gateway 再过滤 |
| `call` | 已授予 capability_id + input；当前 task authority 和 provider 最终授权 |

入网凭据只约束这些 Mesh 额外入口；不改变 Cloud 自己的 Shell/网络权限。
`task_submit` 不是让临时节点变为新 Leader：生成的任务进入既有 owner
Ingress 的固定连续 Leader session；续接保留原 task/native thread，不
重建历史或清除旧 unknown。临时节点 VM 结束后，新工作仍能在 Mesh 存续。

每个写请求先存完整不可变 intent，然后调用业务；同 ID 内容变化冲突。
已确认请求返回原回执，未知请求只返回 unknown/automatic_retry=False，
不能因掉网/节点消失/切模型重跑潜在副作用。最多 256 个新写 intent/job。
关掉节点 credential 不证明远端任务取消或外部操作闭合；已准入回执可
完成持久记录，但不会把关闭的凭据重新激活。

## 完整成果和验收边界

`artifact` 在写 intent 前严格解码 base64、核对声明 SHA 与完整大小，
原始 bytes 内容寻址、fsync、atomic create 后入 journal；不执行 artifact。
默认单个 4 MiB，可配置至 16 MiB；最多 64 个普通成果 + 一个完整 terminal
result，合计不超过单件上限的八倍。更大/流式成果需要另外的能力适配，
不能假称已支持任意大小。transport body 上限需计入 base64/JSON 开销。

`read_artifact(job_id, name)` / `saved_result(job_id)` 在 VM、凭据、租约
失效后供 owner 完整离线读取，每次核对完整原字节与 SHA；不仅保存摘要。
`jobs.read_diff(job_id, sha256)` 同样可不联网读取已保存完整 patch，不 apply。

`provider_reported=True` 只表示已接收节点自报；`execution_verified=False`
与 `artifact_content_verified=False` 保留，不因为 HTTPS 200、Ready 或
正确 SHA 自动变为独立成果验收。最终是否完成原 Mesh task、释放执行
权和迁移工作仍由现有 authority/独立 oracle 决定。

## 此片验证与实际入网的剩余 gate

局部测试覆盖官方 fake CLI 的一次边界、UI 原回执隔离、同 task/epoch/
lease 认证、过期/撤销、错误 credential、并发 unknown、Gateway/Ingress
gate、固定 owner/project 入口、原 task/native thread 续接以及完整成果
离线恢复。实际 loopback HTTP 测试回传 153600 B 二进制和 12000 行
UTF-8 结果，并验证错误 token 拒绝；它不是官方 Cloud job 或公网 HTTPS。

真实验收还需：同主账号可访问的 published environment；原 task-bound
单次官方 job；VM 能访问的现有 HTTPS gateway；不含本机 auth 的独立
凭据注入；从 VM 发出的实际 hello/能力往返/完整成果；VM 停止后从 Mesh
读回并独立验收。缺任何一项就记录其 gate，不能称“Cloud 已入网”。
