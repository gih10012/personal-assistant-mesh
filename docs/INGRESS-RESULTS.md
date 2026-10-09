# Explicit ingress result publication

这是 PAM-006b 的本地底座：只把 owner 显式审查后选择的有限摘要与
HTTPS 成果引用交给原 ingress subject。它不自动暴露 `task.result`、
checkpoint、native history、凭据或本地文件，也不是已连接本人
ChatGPT/OAuth/MCP/Cloud 的声明。任务提交基础见
[INGRESS-CONTRACT.md](INGRESS-CONTRACT.md)。

## 集成接口和权限

先初始化同一 Store 上的 `Ingress`，再创建：

```python
IngressResults(store, authority_id, ingress_config)
```

`ingress_config` 复用原入口的 owner 配置；现有 ingress namespace 必须
已存在并匹配。constructor 仅创建自己的结果表，不能用它重建、认领
其他 owner 的任务。接口为：

- `publish(authenticated_operator_peer, payload)`：仅已鉴权的 operator。
- `read(authenticated_ingress_peer, {"request_id": ORIGINAL_REQUEST_ID})`：
  必须有 `tasks:read`，只读该 owner/source/subject 的原请求。
- `validate_publication(payload)`：无 I/O 的内容校验，不提供身份或审查证明。

Root 将这些方法集成到受控 HTTP routes。入口、worker、viewer 不可
发布；`reviewed=true` 或 payload 里的 source/subject **不是** operator
身份。source/subject 在发布时是 owner 明确选择的目标；读取时只能
来自已经鉴权的 ingress peer，不能覆盖身份或使用全局 task ID 查询。
operator bearer 不交给 ChatGPT/临时节点。

发布 payload 恰好有以下字段：

| 字段 | 含义 |
| --- | --- |
| `publication_id` | 发布前保存的稳定 ID |
| `source`, `subject`, `request_id` | 原入口绑定的精确目标身份/请求 |
| `task_id`, `task_epoch` | 原 task 身份与完成 epoch，不允许换 task |
| `task_result_sha256` | owner 审查时原 result 的 UTF-8 字节 SHA-256，服务端实际计算核对 |
| `reviewed` | 必须是真正 boolean `true`，记录 owner 的发布选择 |
| `result` | 独立准备的 `summary` 与可选 `artifacts`，不是原 result 自动转换 |

`summary` 最多 4096 UTF-8 字节；`artifacts` 最多 8 个，每项只允许
`url`、`label` 和可选 `declared_sha256`。例如一个声明的成果引用可以是：

```json
{
  "url": "https://github.com/example-owner/example-repo/blob/fixed/report.md",
  "label": "公开成果引用"
}
```

URL 最多 2048 字节，只允许 HTTPS/default 443；拒绝 userinfo、query、
fragment、明显 localhost/私有/保留/IP 映射地址、本地 scheme/path 和
明显 credential 字段。不会解析 DNS、请求 URL、读取本地路径或下载
内容；语法被接受不证明服务器、DNS 或文件实际公开。标称
`declared_sha256` 仅是声明，`artifact_content_verified` 始终 false。

部分明显 Bearer、API key、私钥文本也拒绝，但这不是完整 DLP。
owner 仍须审查自由文本和 URL path，不把秘密放进摘要/label/引用。
客户端把这些值当数据，而非可执行 HTML、Shell 或网页指令。

## 真实账本绑定、幂等和拒绝漂移

publish 和 read 都在同一 authority transaction 内核对：

1. 原 owner/authority namespace 与 source/subject/request 绑定。
2. 实际 Store task 存在、`completed`、已被 claim（attempts/epoch 正值且
   node 存在），并有非空 result；仅手写 completed 状态不足以发布。
3. 实际 result 字节 SHA 与审查时预期值相同；原 request fingerprint、
   task epoch、attempts、node digest 与发布 anchor 一致。

这个核对证明**账本绑定与内容 CAS**，不是实际模型运行、外部副作用
成功或结果质量的独立证明。原生/工具/业务成功仍需对应执行证据。
原 result 超过 8 MiB 时不读取/截断来绕过绑定，而是拒绝该发布。
私有 SHA/identity/anchor 不返回给入口。

一个原请求目前只允许一个不可变 publication。同发布 ID/同规范正文
重试返回旧记录；修改正文、引用或目标冲突。另换 publication ID 也
不能替换同一请求的成果。提交/读取都不启动模型、不改 task 状态，
不重做未知效果、不释放资源，也不发送通知。插入或 readback 失败
整笔回滚，保留任务/result/effects 原账本。

发布后 task 变为 needs_review、result/epoch/node/attempts 或绑定改变，
读取和原 ID retry 都拒绝；不能把旧成果冒充当前成功。没有自动修订
或迁移，也不从 task.result 回退取值。未发布、不存在与跨 subject
读取都返回同一 PermissionError（HTTP 403）；未知身份不获全局目录。

成功响应只含 request/task/publication receipts、显式审查的 result
与保守 flags。`review_verification=authenticated_owner_approval` 表示
鉴权 owner 作了选择，**不是** `reviewed=true` 自报被升级为独立验收。
`task_binding_verified=true` 只表示上述 authority 关联；
`execution_verified=false`、`artifact_content_verified=false` 始终保留。

## 当前验收边界

31 项本地测试使用真实 Store heartbeat/claim/update 流程的 fixture，
覆盖未 claim 的 completed 拒绝、实际 SHA/CAS、状态/结果漂移、原子失败
回滚、双实例并发重复、身份/scope 隔离、不重放、无网络/文件查找及
敏感引用拒绝。它们不证明本人账号已接入或引用内容已独立验证。

未实现内容存储/下载、独立 artifact 验证、成果修订/撤销协议、实时
steering、OAuth/MCP transport、临时 Cloud job、账号验证和周期任务。
这些仍是完整 PAM-006 的后续工作，不用本片缩小整个 Mesh goal。
