# Node-owned agent runtime

每台获授权的终端都可以有自己的 agent、账本和原生会话。在线时它作为 Leader 能调用的并行 agent；找不到 Leader 时仍接受本人本地工作、记录故障、运行已有且可用的模型，并按持久退避重新寻找**已配置**的 peer。脱网不是自动成为全局 Leader，也不意味着未安装的离线模型突然可用。

`assistant_mesh.node.Node` 是实际监督器，`run(config, config_path)` 是 CLI 入口：

```bash
python3 -m assistant_mesh --config /absolute/private/node.json node
```

它可以拥有本节点的 loopback `serve` 和 `Worker`，也可以作为既有 authority 的 companion，只监督组网，不启动第二个 server/worker。ClawBot、ChatGPT Live 等属于用户通信入口，**不是此节点的工作区**；节点网络不依赖任何入口持续在线。

## 两种终端间沟通

| 路径 | 含义 | 本节点监督器如何处理 |
| --- | --- | --- |
| A2A：`mesh-a2a/1` | 已认证 agent 消息、持久任务回执和结果 | 固定 sender/destination、稳定消息 ID、同内容幂等接收、原生连续会话；这是项目自有协议，不冒充 A2A 标准 |
| agent → SSH/其他执行通道 → 机器 | 在目标机执行本人授权范围内的命令 | 独立执行账本和权限；不能据 SSH 可达性声称远端 agent/Leader 已上线，未知执行结果不自动重放 |

监督器使用 A2A 的私有 RPC，跨机必须是 TLS 或预先建立的 SSH loopback 隧道。它不会扫描主机、新增 ssh alias、跳过 host key、擅自安装模型、提升权限或购买算力。SSH 通道见 [NETWORK](NETWORK.md)。

## 私有配置合同

以下是结构示例，不包含真实凭据。配置和 token 放在仓库外的本人 `0700` 目录，普通文件 `0600`、不可是 symlink。参数中的路径需改成部署环境的**绝对路径**。`node.json`：

```json
{
  "node_id": "laptop",
  "local_server_config": "/absolute/private/laptop-server.json",
  "local_worker_config": "/absolute/private/laptop-worker.json",
  "start_server": true,
  "start_worker": true,
  "poll_interval": 1,
  "auto_maintenance": true,
  "reconcile_remote_children": true,
  "peers": [
    {
      "node": "cloud",
      "authority": "cloud",
      "client_config": "/absolute/private/laptop-to-cloud.json",
      "report_results": true
    }
  ]
}
```

`laptop-server.json`：

```json
{
  "node_id": "laptop",
  "database": "/absolute/private/laptop-ledger.sqlite",
  "port": 17681,
  "peers": [
    {"role": "operator", "token_file": "/absolute/private/local-owner.token"},
    {
      "role": "worker",
      "node": "laptop",
      "token_file": "/absolute/private/local-worker.token",
      "capabilities": ["agent", "mesh.node:laptop", "a2a.send"]
    },
    {
      "role": "agent_peer",
      "node": "cloud",
      "token_file": "/absolute/private/cloud-to-laptop.token",
      "capabilities": ["a2a.delegate", "a2a.report"]
    }
  ],
  "network_peers": {"cloud": {"send_allowed": true}}
}
```

`laptop-worker.json` 复用现有 Codex/Pi worker 配置，仅让其 `control_url` 指向本节点 authority：

```json
{
  "node_id": "laptop",
  "control_url": "http://127.0.0.1:17681",
  "token_file": "/absolute/private/local-worker.token",
  "capabilities": ["agent", "mesh.node:laptop", "a2a.send"],
  "codex": {
    "executable": "/absolute/path/to/authorized/codex",
    "workspace": "/absolute/authorized/workspace",
    "auth_home": "/absolute/authorized/codex-home",
    "model_policy": "catalog-first",
    "sandbox": "danger-full-access"
  }
}
```

原生 Shell、联网和其他 runtime 功能保留；Mesh 是额外提供的全网受管能力入口，不是限制本机工具的门禁。意外脱网后 agent 仍能自主联网、排障、尝试其他路径重并网；新路线核验后可以登记成推荐/调优的 Mesh 能力。远端 Mesh 调用检查认证和授权，不阻断原生功能。见 [核心合同](MESH-PRINCIPLES.md)。Codex 的实际启动命令和认证目录要沿用本人已授权部署，不能只把示例路径原样填进去；默认不接新收费 API。`laptop-to-cloud.json`：

```json
{
  "control_url": "http://127.0.0.1:17680",
  "token_file": "/absolute/private/laptop-to-cloud.token"
}
```

这里 `17680` 是已配置的 **SSH 隧道本地端口**，不是明文跨网地址。cloud authority 必须把该 token 绑定到 `role=agent_peer,node=laptop`，并明确授予 `a2a.delegate`/需要时 `a2a.report`。token 是文件引用，不进入模型提示词、命令行或公仓。

### 云端访问 laptop：私有 Unix reverse endpoint

反向入口不开放 TCP。cloud `laptop-client.json` 结构如下，替换为 cloud 本人绝对路径：

```json
{
  "control_url": "http://127.0.0.1:17681",
  "token_file": "/absolute/private/cloud-to-laptop.token",
  "unix_socket": "/absolute/private/laptop-a2a.sock"
}
```

`unix_socket` 存在时只有 AF_UNIX 通道；`control_url` 仅作 loopback 协议元数据，不连接它的 TCP 端口。socket 每次请求核验本人所有、0600、直接父目录0700、所有祖先无 symlink；缺失或权限不正确直接拒绝，不使用环境代理、重定向或 TCP 兜底。响应读取上限8MiB。

先部署新的 Client 和 `check_reverse_socket.py`，再安装 [reverse service](../deploy/assistant-mesh-reverse-tunnel.service)。云端实际 `StreamLocalBindMask` 须产生0600 socket，客户端选项不是远端验证替代。启动前 guard 只删除此精确路径下本人私有且连接明确返回 ECONNREFUSED 的 stale socket；活连接、普通文件、symlink 或任何不确定错误保留并拒绝启动。不能用 `rm -f` 或 ssh 全局 `StreamLocalBindUnlink` 绕过保护。云端 `GatewayPorts=yes` 时 `-R 127.0.0.1:端口` 也不能保证私有，因此不得启动旧 TCP reverse unit。

中央服务器 companion 使用自身 `node_id`、既有 `local_server_config`/`local_worker_config`，并设置 `start_server=false,start_worker=false`。此时可以引用中央既有 Leader worker 的配置；监督器不会启停它，不复制 ClawBot receiver。既有 worker 和 server peer 仍须包含自身 `mesh.node:<node_id>` routing capability。

嵌入模式的 Worker/授权 peer 禁止 `leader` capability，只有本人固定 `node` 和本机 routing capability；配置中的 local-worker token 必须只匹配 server 中一个正确的 worker peer。**`mesh.node:*` 是任务目的机器的 fence，不是模型工具白名单，也不是性能等级**。性能、模型、资源和可达路径在动态能力目录表达，而不靠硬编码角色排代。

## 连续记忆、自治和恢复

模型 runtime 的服务模板不额外设置 `NoNewPrivileges` 或 `PrivateTmp`，不会把 Mesh API 策略变成原生工具的进程权限/临时目录隔离。它仍以本人账户运行并受该账户既有 OS 权限约束，不新增 sudo 授权。只运行控制面或 SSH transport 的服务与模型 worker 分离。

- 本地/A2A 任务的 native scope 由目的节点、认证主体、项目和 agent 身份组合哈希，支持同类项目连续 native thread，不与 `leader:owner` 或其他 peer 的记忆混用；Codex 原生 compaction 仍负责压缩。
- 维护任务使用本节点独立 scope/memory scope、固定目的机器 routing capability。每个故障 episode 只创建一个任务，重试不会每轮新建“修复”任务。一次完整健康轮次才结束 episode；只收到 `hello=200` 不足以证明 delegation/report 权限正常。
- 每次连接必须核对 `node`、`authority`、`protocol` 和 `a2a_idempotent_receive=true`。Leader epoch 与相对剩余租约在本地时钟检查；旧、冲突或不匹配公告进入退避，绝不自行改全球租约。
- 连接失败、认证拒绝、协议失败、操作失败都有持久指数退避和确定性 jitter，进程重启也不能绕过退避。hello 成功不会重置尚未解决的操作失败计数。
- 回执丢失可恢复**相同 ID 和相同内容**的 A2A 消息，因为目标接收合同已验证；这不意味着 native 模型执行 exactly-once，更不能套用到 SSH 命令。
- 首次明确 `403` 或首次发送前本人撤回发送授权，持久标记 `denied`，停止自动发送；如果之前已尝试且结果未知，后来的 `403`/撤权仍是 `unknown`，不能声称先前没执行或发新 ID 重跑。
- 开启 `reconcile_remote_children` 时，只有监督器的已认证、owner-bound `/mesh/task` 读取匹配持久 acceptance receipt，才把既有账本代理子任务结算并唤醒等待父任务；未经验证的 report 只是证据。确定初次未交付会变 `needs_review` 而不是“远端完成”，结算前崩溃可重启恢复。
- 节点只汇报源自该 peer 的 A2A 任务，不把本人本地自主任务广播给任意 Leader。report 被明确拒绝后不会不停重发；未知报告用同 ID 恢复。
- Worker 构造失败/异常或意外正常退出也持久退避；稳定运行至少 30 秒且权威账本看到本机 heartbeat 后才清掉 runtime failure。owned authority 死亡会使监督器失败并由 systemd 重新拉起，不假报在线。

`node_runtime.model_availability=not_verified_by_transport` 是刻意保留的边界。没有可用 backend 时，实际 Worker 将任务保持 `waiting_backend`/`waiting_auth`，而不是返回虚构结果；本地记账、诊断任务准备、确定性连接检查仍可进行。有预配置且可用的离线模型，才可能继续模型推理。安装离线模型、收费 provider、自动选最佳调度和技能发布回滚仍需各自实现/实测。

## 服务与验收

参考 [node service 模板](../deploy/assistant-mesh-node.service.example)，先改部署路径再安装为新的本人 user service，不覆盖既有服务。它不 `Requires` tunnel/global authority，因此脱网照样启动；退出只关闭自身 child/server，并释放私有 supervisor flock，不能按名字杀掉别人的进程。

```bash
python3 -m unittest discover -s tests -p test_node.py -v
```

测试在临时本人私有目录启动真实 loopback HTTP authority，覆盖：断网/重连和本地租约、跨节点目的机器 fence、身份/协议/幂等接收合同、真实回执丢失防重复、操作退避/故障 episode 防风暴、权限撤销、remote proxy receipt-bound 结算和父任务唤醒、未知拒绝不能假结算、私有配置与 token、owned supervisor 锁、runtime failure，以及实际 Worker 无 backend 时 `waiting_backend`。模型结果处的 ledger fixture **不是离线模型推理的验收**，也不能据 loopback 测试声称云端双向组网已部署或手机已收到消息。真实部署/跨机证据另外记录在 [ACCEPTANCE](ACCEPTANCE.md)。
