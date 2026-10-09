# 能力目录联邦与重汇合合同

对应 PAM-003a/003b/003c/003d，代码核对日期：2026-10-09。
当前已实现可选导出、只读投影、认证拉取、Node 接线及模型/CLI 查询。
**已在正式 VPS↔laptop 加载可选双向 Node 同步，连续认证读取 `ok`；
正式 Registry 当前为空。** 此前隔离临时 SSH forward 断连/冷恢复/重联
已实际验收。两项证据分开，不证明实际 Wi-Fi 切换或全网自主恢复，
详见 [实测记录](ACCEPTANCE.md)。目录联邦不是
全局 HA、跨 authority 共识或自主最佳调度；研究依据见
[FRONTIER-MESH-RESEARCH](FRONTIER-MESH-RESEARCH.md)。

Mesh 只增加受管能力入口，不拦截原生 Shell、文件、网络或 MCP。目录
失败只降低相关证据的可用性，不禁止节点 agent 自行排障、联网、创建
工具或使用本人另行授权的原生路径。原生连续会话、goal/plan 与任务
账本保持原合同，见 [原则](MESH-PRINCIPLES.md) 和 [两条通信通道](NETWORK.md)。

## 实际数据路径与权限

```text
已 enrollment 远端 authority 的 Registry / audit
  → GET hello：核对配置 node / authority / protocol
  → GET capability-export：一轮有界一页
  → 本地独立 projection 表 + 同事务游标
  → mesh federated_capabilities / CLI：只读候选证据
  → 模型明确 remote_delegate
  → 目的 authority 的本地 child / 资源 owner 准入与结算
```

| 接口 | 开启条件与读取权限 |
| --- | --- |
| `GET /v1/mesh/capability-export?after=0&limit=100` | `export_enabled=true`；operator/viewer，或有绑定 node 且授予 `capability.catalog` 的 worker/agent_peer |
| `GET /v1/mesh/capability-projection` | `projection_enabled=true`；operator/viewer，或有绑定 node 的 worker/agent_peer |
| `GET /v1/mesh/hello` | 已认证 mesh member；同步必须验证返回的 node、authority 和 `mesh-a2a/1` |

所有接口沿用私有凭据认证。远端 `capability.catalog` 只允许读取目录，
不是 `a2a.delegate`、本地任务领取、owner 审批或资源使用授权。
没有 projection 导入 HTTP 写接口；导入由已配置 Node synchronizer 执行，
模型不能在请求中任意指定可信 issuer。两方向同步需分别配置。

当前 server 的 source `issuer` 来自部署 `node_id`，`hello.authority` 也
返回该 `node_id`。peer 的 `authority` 必须与其一致；不能只改请求体
声称另一 authority 身份。仅建立 TLS/SSH 连接不代表信任新的 peer。

## 导出：最新状态增量 snapshot，不是 history feed

[FederationSource](../assistant_mesh/federation_source.py) 接收已初始化
Registry，只读取核心表。一次 Store transaction 取得 audit head、每个
实例最近一次 advertise/renew/revoke sequence 与当前公告，按 sequence
升序分页。相同 capability epoch 的 renew/revoke 仍具有新的 revision。

`format="mesh-capability-export/1"` 的精确页字段：

| 字段 | 含义 |
| --- | --- |
| `issuer` | 部署绑定的源 authority |
| `after` | 本次请求的已消费游标 |
| `source_sequence` | 同事务的整个 capability audit head |
| `next_cursor` | 未完成页最后返回 revision；完整页为 head |
| `complete` | 本次 snapshot 在 head 范围内已无后续待返回行，不表示全网完整 |
| `exported_at` | 同事务一次源端 clock 采样，用于声明式时效估计 |
| `records` | `[{id, revision, capability}]`，明确当前公告或 revoked tombstone |

`capability` 与 Registry 公共 `_view` 形状一致：id、principal、kind、
description、spec、epoch、health、deadline、revoked、created、updated、
secret_fields_redacted、available、lease_expired、verification、
health_verification。verification/health_verification 均为 `declared`。
不导出独立 metrics、资源图、grant、pool、task、认证、模型状态或效果账本。
spec 中常见凭据字段与 URL userinfo/query/fragment 再次脱敏；自由文本
不是完美 DLP，provider 仍不得把秘密放入描述、路径或任意字符串。

- revision 不是执行 epoch；非导出 audit 事件造成 sequence 间隙，合法。
- 每页 limit 为 1–1000，默认 100；source/projection 的规范 JSON 页上限
  8MiB、单 record 64KiB。达到页边界返回部分页，不静默跳过实例。
- 一个实例多次变化可合并为当前最新行；没有逐历史 after-image 重放。
  页缺席从不代表删除，撤销必须有明确 tombstone。
- `after > head` 拒绝，不自动清空游标重来；源行没有 mutation revision
  也拒绝。没有 incarnation 协议，不能识别所有数据库旧备份回滚。

## 投影：保留来源，不复制权限或容量

[Projection](../assistant_mesh/federation_projection.py) 使用独立 SQLite
表，按 `(issuer,id)` 隔离公告，记录历史 revision 指纹、page receipt 和
每 issuer cursor。本页记录与游标同事务提交；失败不部分推进。

相同 ID、不同 issuer 不合并。相同 revision 的不同稳定内容、旧 head、
不匹配 cursor、principal 改换、epoch 倒退或已知 tombstone 复活均冲突。
已知重试不覆盖更新事实，不延长该 capability 的原接收年龄/估计期限。
`available`、`lease_expired` 随导出时间自然变化，不纳入同 revision 的
稳定内容指纹；deadline、spec、health、principal、epoch 等仍参与。

新公告的本地期限估计为接收时间加
`min(86400, max(0, source_deadline-exported_at))`；同时需要近期连接证据。
**这不是跨机器时钟证明，也没有扣除未知传输延迟。** 返回明确标记
`freshness_verification="declared_estimate"`、
`transport_delay_accounted=false`、`source_clock_verified=false`。
新成功请求可以更新连接证据，但不能替 capability 续租。

查询默认隐藏撤销、失联、stale、估计租约过期或源健康 failed/unavailable
的记录；`include_unavailable` 可查看原因。外层 `available` /
`remote_delegation_candidate` 是本地当前估计，内层 capability 是保存的
源公告，不应将其中历史 available 当当前事实。所有结果保留：

```json
{
  "managed_invocation_authorized": false,
  "local_registry_imported": false,
  "global_consensus_verified": false
}
```

远端公告不进入当地 Registry，不成为当地 grant/pool/capacity。
模型可据此比较、探测、选择节点；实际执行仍经
[remote_delegate](A2A-DELEGATION.md)，由资源 owner authority 检查真实
授权、任务 fence、容量和副作用。共享账号或同一出口不能因多个投影被
重复算作多份资源。重联不换 message/operation ID 绕过 unknown。

## 可选私有配置

以下是需要合并到现有配置的通用结构示例，**不要替换真实节点配置**。
Root 已在两个现有节点合并相应开关与目录权限，保留原身份与通信路径。
保留现有身份、A2A grants、worker/runtime 和其他配置；不要用片段覆盖
完整文件。所有配置/token 位于仓库外的本人 0700 目录，文件 0600，路径
为部署环境真实绝对路径，见 [NODE-RUNTIME](NODE-RUNTIME.md)。

源端 server 开启导出，并给需要读取它的已 enrollment peer 增加目录权：

```json
{
  "node_id": "remote-node",
  "federation": {"export_enabled": true, "projection_enabled": false},
  "peers": [
    {
      "role": "agent_peer",
      "node": "local-node",
      "token_file": "/absolute/private/local-to-remote.token",
      "capabilities": ["capability.catalog"]
    }
  ]
}
```

接收端 server 合并 `"federation":{"projection_enabled":true}`。
两个开关省略时均关闭，可独立开启；投影不自动反向导出远端内容。
Node 给既有 peer 增加 `sync_capabilities`，例如：

```json
{
  "node_id": "local-node",
  "local_server_config": "/absolute/private/local-server.json",
  "local_worker_config": "/absolute/private/local-worker.json",
  "federation_sync": {
    "interval_seconds": 15,
    "page_limit": 100,
    "freshness_seconds": 30
  },
  "peers": [
    {
      "node": "remote-node",
      "authority": "remote-node",
      "client_config": "/absolute/private/local-to-remote.json",
      "sync_capabilities": true
    }
  ]
}
```

`sync_capabilities` 默认 false；选中 peer 需要本地 server 配置明确
`projection_enabled=true`，否则启动拒绝。同步 interval 1–300 秒，默认
15；page_limit 1–1000，默认 100；freshness_seconds 1–86400，默认 30。
freshness 是近期连接证据窗口，不是给远端资源新增使用授权。

既有 client 配置仍是 `control_url`/`token_file`，可使用已经批准的私有
Unix socket；HTTPS 或 SSH loopback 路径沿用现有合同，不安装新 transport，
不扫描主机、不创建密钥、不允许重定向/环境代理携带 Mesh token。
示例进程方式是现有 Linux 部署适配，不把该方式要求施加给所有移动/
Windows 节点；其他环境仍需要实际 adapter/enrollment 验收。

## 同步运行、查询与诊断

[FederationSync](../assistant_mesh/federation_sync.py) 每次到期只做 GET
hello 和一页 GET export；收到部分页后在下一轮继续。Node 的独立目录
线程依次处理选中 peers，不在原生 worker/A2A 主循环中阻塞拉目录。
共享 SQLite 仍有正常事务竞争，独立线程不是零延迟或性能保证。

失败持久记录固定诊断类别和退避；指数退避由 2 秒递增，当前封顶 256
秒。cursor 与退避重开保留。失败标记目录连接不可用，不取消原生工具、
改写 A2A 权限、复制任务或重放效果。`Node.status().federated_capabilities`
可观察 peers 状态；`running` 只说明循环状态，不证明同步成功。
认证/目录权限拒绝、路由缺失、身份不符、投影冲突与连接不可用分别
记录为 `catalog_*` 类别，不把私有响应或原始异常当公开诊断。

已部署相应版本与私有配置后可使用以下只读命令。Root 已在两个正式节点
实际读取 export/projection，并从持久同步状态核对多轮认证拉取；下方仍是
通用路径示例，不包含私有配置：

```bash
python3 -m assistant_mesh --config /absolute/private/local-operator.json mesh-capabilities
python3 -m assistant_mesh --config /absolute/private/local-operator.json mesh-capabilities --issuer remote-node --include-unavailable --limit 100
python3 -m assistant_mesh --config /absolute/private/local-to-remote.json mesh-hello
python3 -m assistant_mesh --config /absolute/private/local-to-remote.json mesh-capability-export --after 0 --limit 100
```

CLI 只读 export 不导入该页。Node 才按本地已保存的 issuer cursor 拉取，
不要在出错后手动清表/改 issuer 来制造“成功恢复”。模型查询入口：

```text
mesh(action="federated_capabilities",
     arguments={"issuer":"remote-node","include_unavailable":true,"limit":100})
```

`issuer`/`kind`/`include_unavailable`/`limit` 都是读取过滤器，不是 actor
覆盖。目录不可用时模型可使用其他已授权原生路径，但不能从目录文字
推断远端实际推理、联网、性能或执行完成。

## 验收边界与下一步

source/projection 的临时 SQLite 测试覆盖同 epoch 更新、tombstone、
有界页、并发源 snapshot、重开、原子游标、重复不续租、issuer 隔离与
冲突拒绝。实现与夹具证据不能代替正式部署或真实双端故障证据。
Root 已完成隔离两端目录链路验收、公开发布及正式双向循环加载。原生
Leader 又在同一 thread 经 A2A 委派 laptop 采样，原父任务真实等待并续接。
正式目录仍空，网络报告未登记；有内容的持续目录/出口和故障域观察
按 PLAN/TASKS 的下一项推进，不重复破坏正在工作的节点做验收。

下一条真实链路需记录断连前后的来源/游标、局部任务连续性、重联回读
原 ID、撤销不被旧页覆盖及 unknown 无第二次执行。尚不包括完整历史
事件 feed、incarnation/所有旧备份回滚检测、离线授权共识、跨 authority
原子容量预留、标准 A2A conformance 或全局 HA。
