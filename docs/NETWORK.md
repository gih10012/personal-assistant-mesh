# 终端自治与两条通信通道

设计依据是本人提供的 agent mesh 文稿/PDF 的任务、能力、执行节点和可达路径四层分离，以及当前持续 Codex 会话控制需求。私人原文和聊天内容没有复制进公仓。ClawBot/Chat/Live 是通信能力，不能变成 Leader 工作区。

```text
本人 → 任意获授权入口 → 持续 Leader / 原生 goal、plan、记忆
                         ├─ A2A → 对端自治 agent → 对端本地账本/原生会话
                         │                   └─ 回执核对 → 原账本子任务 → 原 Leader 续接
                         └─ SSH 等执行通道 → 远端终端/能力 → 独立结果/不确定性记录

终端脱网 → 本节点账本与会话继续 → 有可用模型才推理
         → 确定性重连检查 + 去重维护任务 → 已授权 peer / Leader
```

## A2A 不等于远端 shell

`mesh-a2a/1` 是本项目实现的 agent 协作合同：固定认证 sender、明确目的节点、项目/agent、不可变 message ID/内容、原子本地接收、可查询的任务结果。当前**不是**标准 A2A v1 的 wire-compatible server。标准 A2A 的认证与权限仍由服务端策略负责，SendMessage 并不自动提供强幂等；接未来官方 SDK 前需做版本和 conformance 验收，不能改一个名称就冒称兼容。[A2A 规范](https://a2a-protocol.org/latest/specification/)

接收任务只给目的节点的本地 worker 领取。`mesh.node:<node>` 是目的机器 fence，不是性能评级或模型工具白名单。sender/node/role/审批资格由受保护部署配置绑定，入站 JSON 不允许自行选择。`agent_peer` 无权领取或改写本地 worker 租约，viewer 不可委派。

默认连续 native scope/memory scope 同时绑定目的节点、认证 peer、项目和 agent，使用结构化哈希避免冒号碰撞。跨 peer 不共享 owner 偏好和其他 peer 的记忆。自己的持续 Leader 仍保留原来的 native thread；跨主机/账户恢复只传选定原生 rollout，由 Codex 做 compaction，不重写摘要。

远端委派创建的是现有 `tasks` 表中的代理 child：`waiting_remote` 不会被本地 worker 误执行。只有经认证的结果查询匹配已保存的 destination/message ID/acceptance receipt/remote task ID，才能结算 child 并自动唤醒等待中的**原父任务**。任意 report 只是证据，不能凭其文字完成全局任务。见 [委派合同](A2A-DELEGATION.md)。

## SSH 是独立执行通道

`SSHExecution` 只用本人配置的 SSH alias、host-key 校验和批处理认证；不扫描、不自动加主机、不改防火墙、不装模型、不买算力。scope 必须匹配已授权私有配置或独立授权回调，默认拒绝。它不限制 agent 已获授权的原生终端；它为跨主体执行提供明确的记录边界。

请求在网络操作**之前**写 execution journal。命令以 argv 构建，逐参数引用后给远端 shell，不能在本地执行 `shell=True`。结果保留真实退出码和有上限的输出；SSH 255、超时、进程中断等无法判断远端效果时保持 `unknown`。重复 request ID 不再启动命令；不能因为本地无结果就换 ID 重跑。原生 shell 本身仍能产生副作用，状态记录不等于保证 exactly-once 或 undo。

只有连接的机器未必有 agent；只有 SSH 成功也不能宣称 A2A/Leader/模型在线。连接能力与其时延、健康、路径、权限分别登记；CPU/GPU/模型/工具性能保持带单位、采样时间、来源和证据的动态指标，而不是固定设备等级。见 [能力设计](CAPABILITY-DESIGN.md)。

## 脱网与寻找 Leader

每个本节点可以有 own SQLite authority、worker、连续原生会话和维护 agent。与全局 Leader 失联不会销毁这些本地工作；它也不会复制未核对的全局任务、夺取全球 Lease，或重放网络失联前的 SSH 效果。没有可用离线模型时，记账/确定性重连仍运行，模型任务进入 waiting 状态，不能假称完成推理。

监督器验证已授权 endpoint 的 TLS/SSH loopback、身份、authority、协议、receiver 幂等合同和相对租约；不跨机直接比较绝对时钟。认证失败、协议失败与 operation 失败均持久退避，重启不清零；每个 episode 只产生一个本地维护任务。只有完整健康轮次才结束 episode，`hello=200` 不等于发送权限/模型可用。详细运行配置见 [NODE-RUNTIME](NODE-RUNTIME.md)。

稳定 ID 的 A2A 重试只有在**本接收端合同实际支持原子防重**时成立。首次明确拒绝且没有此前不确定提交，才可证明没接收；丢过回执后再遇拒绝仍为 unknown，不能伪称未执行。恢复后只重用同内容同 ID。私有工作不广播；只向原委派 peer 反馈属于它的任务。

## 当前部署路径

本人现有 laptop 和 cloud 两个节点：本地独立节点端口 17681，云端原控制面 17680。前向 SSH 只监听 loopback；反向 SSH 使用云端本人 0700 私有目录中的 0600 Unix socket，不开反向 TCP 端口。该选择避开云端既有 `GatewayPorts=yes` 强制公网绑定，无需改全局 sshd。socket 缺失或不私有时拒绝，不回退 TCP。云端 companion 不重复启动 iLink、authority 或 worker。新节点服务不覆盖旧服务或 desktop 登录。

`scripts/configure_nodes.py` 显式生成/更新私有配置并先备份现有配置；不自动联网发现凭据或授权陌生节点。示例脚本只演示这两台已授权机器的 enrollment，不是模型能力、整个网络拓扑或未来平台列表的上限。其他终端可以通过同协议及明确 enrollment 接入，手机/蓝牙/LAN 网关仍各有平台和权限验收。

## 证据与未完成

网络、权限、幂等、维护与 remote child 结算用真实临时 SQLite/loopback HTTP 和并发测试验证。模拟 backend 与 ledger fixture 不是脱网模型推理的证据；服务 active 也不是跨机器委派完成。实际部署/跨机/Live 的结果另记 [ACCEPTANCE](ACCEPTANCE.md)。

当前依然是有权威的 hybrid mesh，不是多副本共识或完全去中心化。标准 A2A adapter、gossip/enrollment 发现、离线模型、远程取消协议、技能构建/发布/回滚、append-only 完整任务事件与活跃外部效果 reconciliation，仍需具体实现和各自验收。A2A 与 SSH 分工清晰是实现这些能力的底座，不替代它们。
