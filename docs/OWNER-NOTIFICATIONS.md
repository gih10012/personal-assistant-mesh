# 节点向本人回传通知

这是可选的 Mesh 能力，不是原生 Shell、网络、文件、MCP 或模型的权限边界。节点原生工作和重连不依赖该通道在线。ClawBot 仍只是沟通入口，不是 Leader 的工作区。

只有原有云端通道接收/发送微信；节点不会因为回传失败另启本地 iLink 接收器或切换微信发送后端。当前回传仅支持文字，媒体不隐式进入文字入口。

这是当前生产限制，不是最终架构。后续 [PAM-009 多节点承载](COMMUNICATION-CARRIER-HA.md)
使本机/VPS/其他节点成为候选并安全交接账号状态，尚未实现/部署。
不能靠复制 token 或 ping 失败另启接收器宣称容灾。内部 `cloud`
指 Alibaba VPS，不是官方 Codex Cloud。

## 显式通知与自动结果

节点私有配置：

```json
{
  "notification_policy": {
    "mode": "private",
    "owner_relay": {"peer": "cloud", "authority": "cloud"}
  }
}
```

`peer` 必须已经在 node 配置中 enrollment，且 authority 精确匹配。节点 worker 的凭证能力以及云端对应 `agent_peer` 都须由 owner 明确授予 `owner.notify`。这不把 peer 变成 operator，也不授予选择收件人的能力。

显式 `notify` 或 owner 本地通知入队才创建 `delivery_route=relay` 的 outbox 行与同事务的持久 relay intent。自动完成结果、排障结果和中断核对通知在 private 模式仅 `recorded_private`；旧 outbox 行保留原 ID、内容、状态和目的地，不自动补发或 enrollment。没有回传配置的 private 通知也只记录在本机，不声称排入微信队列。

默认 channel 模式保留原单通道服务行为。private 模式不启动微信 receiver。启用通知不会替换连续原生会话，也不会改变 Codex sandbox、已有认证或 native 工具。

## 去重、断网与换账号

1. 本机 intent 固定来源节点、peer、authority、请求 ID 与文字 SHA-256；发送尝试也先持久化。
2. 先用已绑定 peer 凭证查询 `POST /v1/mesh/notify/status`。响应必须精确绑定协议 `mesh-notify/1`、来源、请求 ID、内容指纹、云端 outbox ID 和不含明文账号的 `route_id`。
3. 首次收到的 route_id 在本机持久化后才提交；`POST /v1/mesh/notify` 必须携带它，云端在入队前再次核对。云端回执与通道 outbox 同一事务落盘。
4. 实际 POST 前同事务持久化 `post_attempted=1`。回执丢失、进程崩溃或网络断开后，继续查询同一 ID。只有从未尝试 POST 的 intent 才能首次提交；一旦尝试过 POST，即使后来同账号回复 `not_found`，也只核对、不自动重新入队。

看到过 `found=true` 后又找不到回执，会报 `relay_remote_receipt_missing`；只尝试过提交而未拿到回执时，报 `relay_submission_unconfirmed`。两者都不会重新入队。即使首个 POST 实际未到达云端，这种不确定状态也需 owner 核对后另行决定恢复；不拿静止的 `not_found` 当重发权限。`unknown`、`submitting`、`waiting_auth` 等已有云端记录只查询；不会创建新 ID、改文字或直接再次调用微信发送。通道自身原有的明确拒绝/新上下文恢复合同独立保留。

云端把实际通道 origin、绑定 user/bot 的摘要及随机接收账本实例标识持久化。换 token 不改变账号路由；新账本即使使用同账号也得到不同 route_id；换账号则拒绝加载旧通道绑定，不自动覆盖。更换 peer/authority 也不能把旧本机 intent 改投。实例标识本身不检测同实例历史快照回滚；本机仍保留的 `post_attempted` 会阻止缺失回执后的自动重建。本机 intent 和云端账本一起回滚/丢失并不在防重保证内，备份恢复需要独立审计与隔离，不能自动恢复发送。账号迁移及旧通知处置需要 owner 另外明确决定，不用清空 ledger 绕过。

这不提供通用微信账本反回滚保证：若恢复了实际发送前的旧 `channel/pending` 行，再盲目启动通道发送器，本机 relay 标记也无法阻止通道自行重发。恢复此类快照必须先隔离发送并核对实际副作用；本功能不会自动还原生产账本或解除未知状态。

独立 relay 线程采用私有 flock、事务 claim、持久退避（2–60 秒）与有界 RPC。通知网络失败不进入节点心跳/重连的阻塞路径。停止只在请求边界响应；短暂 join 超时不表示远端发送已停止，更不表示手机已收到。

## 明确的部署操作

先备份私有账本，确认 worker 不在执行本轮有副作用任务；在两端使用相同、已验证的源代码。配置写入自动保留旧私有配置，不读/复制/输出 token。

```bash
# 在云端，对已有 laptop peer 增加这一项能力；可先加 --dry-run。
python3 -m scripts.configure_notifications grant-cloud-peer \
  --server-config /absolute/private/cloud-server.json --peer laptop

# 在本机，固定回传到已 enrollment 的 cloud，并授予本机 worker。
python3 -m scripts.configure_notifications enable-node-relay \
  --node-config /absolute/private/node.json --peer cloud --authority cloud
```

命令只准备配置，不自动重启服务、不恢复/删除历史通知、不发消息、不采购。按原部署 unit 明确重新加载服务后才生效。`--dry-run` 不写入配置；同配置重复执行不制造新 token 或新路由。新的原生线程和连续旧线程均可通过已有 mesh 入口显式 `notify`。

## 状态与验收边界

- `recorded_private`：仅本机记录。
- `queued`：原云端队列已经入账，或正在发送；不是手机送达。
- `accepted`：实际微信协议返回接受，仍不是手机单次收件证明。
- `unknown` / `waiting_auth`：保留原身份和回执，继续核对，不改投。

所有 relay 回执与节点通知观察明确 `delivery_verified=false`。`/v1/mesh/notify/status` 仅暴露身份摘要和状态，不输出文字、账号明文、上下文 token 或微信原始详情。对源代码的本地 HTTP/SQLite 测试不等于真实手机收件验收；正式部署和实测范围单独记入 [ACCEPTANCE](ACCEPTANCE.md)。
