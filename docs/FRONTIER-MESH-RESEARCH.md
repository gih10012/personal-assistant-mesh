# PAM-003：前沿 Mesh 研究与第一条实现链路

核验日期：2026-10-09。GitHub 提交与发布时间以下均为 UTC。
状态：**源码研究与建议合同；不是已实现、已部署或全局 HA 证明。**
本文只记录公开来源与本仓源码，不包含账号、密钥、私有节点地址或聊天。

## 结论

保持原生 Codex 为连续 Leader/项目 specialist，Pi 为可选 runtime；不要为了
复用一个网络框架而替换模型大脑。优先做跨 authority 的只读能力投影、
稳定身份和重汇合，再给模型提供证据驱动的候选计划。借用连接/互操作
协议，不把一套固定机器评分或确定性工作流当作所有智能行为的上限。

四种职责不能混为一台“最强 Leader 机器”：

| 职责 | 持有的真相 | 失败时保持的边界 |
| --- | --- | --- |
| 项目/job home authority | goal、plan、任务 ID、审批、原始委派与结果 | 其他节点不伪造其全局写入权 |
| 资源 owner authority | 实际 pool、精确授权、预留、效果回执 | 资源不能通过复制目录变成另一处容量 |
| inference/runtime instance | 模型访问、原生 thread、实际工具和本机环境 | 切模型/节点不能重放 unknown 副作用 |
| 用户入口 | 输入、确认、回复、媒体能力 | Chat/微信/Live 不自动成为权限根或工作区 |

## 当前源码实际具备与缺口

- [Store.elect](../assistant_mesh/store.py)：只在本 SQLite 的节点表内选举；
  不是跨主机共识。同库任务领取、原生会话和 unknown 隔离仍应保留。
- [Node](../assistant_mesh/node.py)：持有本节点 authority/worker，验证已
  enrollment peer，连接失败产生本地维护任务，恢复后继续原 ID 的消息与
  回执对账；没有“替代全局 Leader”机制，也没有跨 authority 能力同步。
- [Registry](../assistant_mesh/resources.py)：公告、观测与精确授权已分开，
  但实例 ID 只在本 authority 唯一。`renew`、`revoke` 不增加 capability
  epoch；epoch 是执行 fence，不能直接当重汇合的完整更新版本。
- [Allocations](../assistant_mesh/allocations.py)：route/pool/观测/任务 fence
  与原子容量准入都在同一个 authority。accepted/running/unknown 不因
  租约到期自动释放，这个效果不确定性边界不能被新调度层绕过。
- Registry 的 `capability_audit` 是操作摘要，不是完整状态事件。
  [API.capability_events](../assistant_mesh/server.py) 默认 recent，现有
  discover 的 LIMIT 也没有完整遍历合同；不能直接声称这是无损同步 feed。
- [NETWORK](NETWORK.md) 的 `mesh-a2a/1` 是本仓协议，不是标准 A2A v1
  wire-compatible server。[原则](MESH-PRINCIPLES.md) 已正确区分局部自治
  与全局一致性；当前中央与本地 authority 是 hybrid mesh。

## 主仓研究：按层复用，不整体换脑

| 主仓 / 许可证 | 核验的主分支提交 | 最新稳定 release | 适合借用 |
| --- | --- | --- | --- |
| [iroh](https://github.com/n0-computer/iroh)，MIT OR Apache-2.0 | [d4490fc](https://github.com/n0-computer/iroh/commit/d4490fcfde8e4dc7260a44a2a982c654748595e2)，2026-10-08 19:10:03 | [v1.3.0](https://github.com/n0-computer/iroh/releases/tag/v1.3.0)，2026-09-28 19:26:13 | 公钥寻址、QUIC、NAT 穿透与 relay/direct 路径恢复 |
| [exo](https://github.com/exo-explore/exo)，Apache-2.0 | [21a54c5](https://github.com/exo-explore/exo/commit/21a54c5ea0230a3bec1e1a786d200126c7e34ec6)，2026-08-25 18:59:53 | [v1.0.71](https://github.com/exo-explore/exo/releases/tag/v1.0.71)，2026-04-23 15:04:10 | 真实内存/模型/拓扑与缓存局部性的 placement 证据 |
| [Restate](https://github.com/restatedev/restate)，BSL-1.1 | [ea8d9ce](https://github.com/restatedev/restate/commit/ea8d9cef6c80a147628947c9b7e05f6344358941)，2026-10-08 17:45:07 | [v1.7.13](https://github.com/restatedev/restate/releases/tag/v1.7.13)，2026-10-01 17:06:19 | 持久决策/调用日志、分片 writer 与实际 epoch 取得 |
| [A2A](https://github.com/a2aproject/A2A)，Apache-2.0 | [12e9d2f](https://github.com/a2aproject/A2A/commit/12e9d2fbb9badfe98f8ab4f660697870eeb1940b)，2026-10-07 19:29:07 | [v1.0.1](https://github.com/a2aproject/A2A/releases/tag/v1.0.1)，2026-05-28 11:34:36 | 跨 runtime task/context/artifact 与远端委派接口 |

这些是匿名读取主仓 API/源码得到的版本信息，不是 installed 版本。
仓库其他分支的 pushed_at 不等于主分支最近提交，release 也不等于 HEAD。

### iroh：优先作为可替换连接侧车

读了 [README](https://github.com/n0-computer/iroh/blob/d4490fcfde8e4dc7260a44a2a982c654748595e2/README.md)
与 [endpoint.rs](https://github.com/n0-computer/iroh/blob/d4490fcfde8e4dc7260a44a2a982c654748595e2/iroh/src/endpoint.rs)。
后者有路径选择、relay 转 direct 测试和协议 multiplex；自定义 transport /
PathSelector 仍标 unstable，不能把它当长期稳定的全平台接口。

采用方式：单个可选 native sidecar 提供 peer endpoint/stream；保留现有
HTTPS/SSH 已授权路径。发现一个公钥不代表已 enrollment，更不授予业务
scope、任务所有权或 Shell 权限。iroh 不提供这些上层政策或作业共识。
[官方语言矩阵](https://docs.iroh.computer/languages) 有 Windows、Android、
iOS 等支持，但不同 binding 范围不相同；Python 不覆盖全部移动平台。
[2026-06 语言说明](https://www.iroh.computer/blog/iroh-language-support)
区分基本流与尚未完整暴露的 gossip/blobs/docs。不得声明手机已实测。

### exo：借算力图，不借全局写入选举

读了 [placement.py](https://github.com/exo-explore/exo/blob/21a54c5ea0230a3bec1e1a786d200126c7e34ec6/src/exo/master/placement.py)
与 [election.py](https://github.com/exo-explore/exo/blob/21a54c5ea0230a3bec1e1a786d200126c7e34ec6/src/exo/shared/election.py)。
placement 将真实内存、模型约束、后端兼容、RDMA/连接和缓存位置带入
候选；可以转化为模型能查询的证据。模型自行选择计划，执行侧验证真实
可行性，不把这一具体优化器硬编码为通用 Leader。

选举源码按候选信息与计时比较；据此**不能推断**它提供本项目所需的
全局线性写入 fence。当前 [平台合同](https://github.com/exo-explore/exo/blob/21a54c5ea0230a3bec1e1a786d200126c7e34ec6/PLATFORMS.md)
主要验证 Apple Silicon；Linux/Windows 路线仍有未完成项。它不是当前
所有 Windows/手机节点的统一原生 agent runtime。

### Restate：持久决策值得借，重试和许可必须保留边界

[多 agent 模式](https://docs.restate.dev/ai/patterns/multi-agent) 与
[remote agents](https://docs.restate.dev/ai/patterns/remote-agents) 展示：
模型选择、调用与结果被记录，恢复不需要重新猜已经记录的路由决策。
本项目可先在现有私有 ledger 中增加持久 plan/proposal，不需先部署框架。

[scheduler.rs](https://github.com/restatedev/restate/blob/ea8d9cef6c80a147628947c9b7e05f6344358941/crates/admin/src/cluster_controller/service/scheduler.rs)
区分目标 leader 与真正取得 epoch 的 observed leader；
[HA 文档](https://docs.restate.dev/server/clusters) 区分 partition 副本
与 quorum log 副本。复制执行者不等于复制持久真相。
[durable steps](https://docs.restate.dev/develop/python/durable-steps)
明确 `ctx.run` 失败默认重试；因此即使借其 journal，也不能将外部 ACK
丢失误当“尚未执行”，更不能取消本项目 unknown 不重放的合同。

[许可证](https://github.com/restatedev/restate/blob/ea8d9cef6c80a147628947c9b7e05f6344358941/LICENSE)
是 BSL-1.1，不是 OSI 开源；每版本发布四年后转换 Apache-2.0，并有公共
Restate 平台服务限制。不要把其服务端源码直接放进本仓当宽松许可代码。
可以借设计或未来选装外部 backend；当前不新增付费基础设施。

### A2A：互操作不自动带来强幂等

读了 [proto](https://github.com/a2aproject/A2A/blob/12e9d2fbb9badfe98f8ab4f660697870eeb1940b/specification/a2a.proto)
与 [规范](https://github.com/a2aproject/A2A/blob/12e9d2fbb9badfe98f8ab4f660697870eeb1940b/docs/specification.md)。
它适合把不同语言/平台 agent 封装为不暴露内部工具和记忆的任务端点。
Send Message 的幂等是 MAY，不是必然保证；AgentCard 也不是业务授权。
未来做正式 adapter 时，仍要保留稳定 ID、接收端原子防重、同 ID 查询，
以及本地原生 context locator；不要拿一段 A2A history 替换连续 Codex thread。

## 第一条可实施合同：enrolled peer capability projection / rejoin

范围先固定为现有已认证的两个 authority，不引入新账号/密钥/费用。
不先做全局选举、跨 authority 原子 pool 或整个节点框架迁移。

1. **专用 source export。** 在 issuer 的单事务内导出安全 capability
   状态与 issuer-owned 单调 sequence。原始 capability epoch 另存为执行
   fence；renew/revoke 也必须可排序。输出有界 page、cursor、has_more /
   complete 和 issuer incarnation。身份来自配置及认证握手，不信 payload
   自称 issuer。重建数据库必须使用新 incarnation，不能让重置的 cursor
   伪装成旧 authority 的继续序列。
2. **源端版本与快照。** 最终事件 feed 应在对应变更事务内记录完整安全
   after-image/tombstone；不要只依赖现有 audit 小摘要。第一片可先做
   有水位的明确 bounded snapshot/upsert，标记 incomplete，禁止从缺席
   推断删除或撤销。完整遍历/增量同步再扩展，不为完整性停住第一条链路。
3. **只读 destination projection。** 独立表按
   `(issuer_authority, incarnation, capability_id)` 保存来源、sequence、
   epoch、tombstone、健康自报、观测来源/单位/workload、接收时间与失效
   期限。本页与 cursor 原子提交；重复同版本同内容幂等，不同内容冲突
   隔离；旧版本不得覆盖撤销。能力类型与 metrics 保持开放。
4. **有限新鲜度。** 不用远端 wall-clock 直接续租本地权限；接收年龄与
   源端剩余 TTL/传输延迟保守计算，无法确定时标 stale。已知更高 sequence
   的同内容 renewal 可以更新观测期限，旧消息/replay 不延长。重启后不能
   把 monotonic 时间重新当全新 TTL。目录在线不证明互联网/模型可用。
5. **模型入口与真实执行。** 给原生 agent 查询本地与 projection 的统一
   evidence view，标明 `invocation_authorized=false`。模型自主选择 probes、
   节点/出口/模型和候选计划；确定性部分只验证身份、授权、epoch、事实
   和预留。Grant/pool/task 不复制成当地权限；远端选择经现有委派产生
   目的 authority 本地 child task，资源 owner 自行 admission/结算。
6. **重联不是新操作。** 先验证 peer/authority，再续同步 cursor，并查询
   原 message/operation ID 对账；不因换模型、路径或 Leader 新建 ID。
   unknown 保持隔离与容量占用。网络分区时各自原生工作和本地 authority
   可继续，不伪造全局独占写入权。新路线可由节点 agent 用原生工具在
   owner 已授权范围探索，Mesh 推荐路径不阻断任何原生能力。

这是一条“来源保留的目录 + 局部 admission”的 federation，不是全网
共识。跨 authority 复合 pool 未来需要资源 owner escrow/有限预留与恢复
协议；共享账号配额/单一出口等瓶颈必须保留同一 failure domain/owner，
不能在不同 projection 中计作多份实际容量。

## Windows / mobile / Cloud 适配边界

先定义平台中立 adapter report：runtime harness、生命周期、原生 context
locator、持久状态位置、实际模型/工具能力、transport、能否入站、输出
与交互。节点 agent 按真实 OS 个性化改装；不要求 Linux systemd/fcntl/
Unix socket 或固定“手机弱、云强”的角色。未验证的 backend 保持未验证。

使用 openai-docs 核验的 [Codex cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environments)
描述每个新 task 的隔离 workspace、继续同 task 的保存状态、网络政策和
可配置 Tailscale VPN。故建议 Cloud 表现为 **job-scoped virtual node**，
由长期 home authority 保存任务真相，以允许的 HTTPS/轮询/实际 VPN
连接；不宣称它是永远存活的 daemon，也未验证 owner 账号是否拥有特定
配置功能。不要套用 legacy cloud 的默认网络假设。
[app-server 生命周期](https://learn.chatgpt.com/docs/app-server#lifecycle-overview)
支持 thread start/resume/fork 与 turn/stream；Mesh 记录原生 locator 和
provenance，不自行重写记忆。Chat/Live 是后续入口 adapter，本文不证明
已完成插件连接、语音桥接或移动原生推理。

## 第一片验收与未做事项

- 本机两私有 fixture authority：同名不同 issuer 不合并；重复 page、旧
  消息、同版本异内容、tombstone、断点 cursor、重启与 TTL 均有检查。
- 已 enrollment 两端真实断连/重联：本地既有能力可继续，原 ID 可对账，
  unknown 无第二次执行；真实证据与测试夹具分开记录。
- 模型查询真实 projection 后形成一个持久候选/选择理由，目的资源 owner
  验证后才执行；失败只改相关计划，不替换 job 身份。
- 未做：iroh 部署、Windows/移动端实测、Cloud enrollment、完整 A2A
  conformance、跨 authority pool 原子准入、quorum HA、全局性能最优。
  研究文件不能充当这些执行证据。由 Root 更新 PLAN/TASKS 的对应状态。
