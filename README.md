# Personal Assistant Mesh

面向通用个人助理的持久执行与能力网络。以 Dots 的长任务、连续上下文、主动协作为产品对标，使用 **Codex 原生 app-server 为主、显式配置的 Pi 为备选**，不是 OpenClaw/OpenCode 扩展，也不是 OpenAI Dots 的源码。

产品入口基于真实 fork：[personal-assistant-dots](https://github.com/gih10012/personal-assistant-dots)。控制面不依赖面板在线；面板是任务、能力和成果的观察窗口，不是模型的工具白名单。

## 设计方向

任务、能力、机器和网络路径解耦。CPU/GPU、模型推理、工具、存储和可达性都应成为可发现、可度量、可组合的能力；质量指标要有单位、样本时间、来源和验证证据，不能把节点自报当成实测。

模型保留原生终端、任意 Shell、文件、联网和 MCP 能力，自行发现资源、创建工具、测试与部署。Mesh 是额外暴露给 agent 的全网能力入口：管理其中能力的发现、权限、路径推荐、组合与调优，**不拦截或替代原生功能**，不要求本机工具全部先注册，也不做固定工具白名单。节点意外脱网后，agent 仍能自己联网、排障、尝试多种路径重新并网。完整目标合同见 [原生自治与 Mesh 能力入口](docs/MESH-PRINCIPLES.md)。

Mesh 内的资源公告不自动授予其他主体使用权，远端调用仍检查授权、额度与副作用状态；这些规则不是整台终端的 Shell/网络隔离。新节点先沿加入通道建立 Mesh 通信，其他线路可验证后加入受管能力目录；这不禁止 agent 自主使用原生线路恢复连接。零支出预算、owner 身份和未决外部副作用不能由模型的自然语言声明替代。

## 已实现的执行基础

- 标准库、SQLite WAL、单权威事务账本；无 Redis 或常开桌面依赖。
- 独立云端微信 iLink 入口：整批消息、任务、上下文和游标原子落盘；稳定出站 ID，未知结果不重试。
- 节点能力目录、Leader/任务租约和双代 epoch fencing；任务绑定实际 Leader 任期，同名节点重新当选也不能续写旧任期任务。
- Leader 原生 thread 跨任务连续，子 agent 按项目/身份复用；上下文压缩由 Codex 自己维护。
- 只传输选定的原生 rollout 用于跨机恢复，不复制整个认证目录或其他聊天。跨机实际验收见 [ACCEPTANCE](docs/ACCEPTANCE.md)。
- Native memories 通过进程级配置开启；后台生成有 idle/quota 条件，不等于每轮即时写入，也不等于 mesh 手工事实记忆。
- 模型创建持久子任务、等待结果后自动唤醒；没有硬编码的委派深度或工具白名单。
- 原生 `/plan`、`/goal`、`/steer` 接线；问题和单次审批可持久回复。
- Codex 启动/认证不可用且尚未开始执行时，才尝试配置好的 Pi；不跨 harness 重放已起效 turn。Pi 真实兜底仍需配置/验收。
- 复用已有 Codex 认证，从账户实时模型目录选择，不要求在面板粘贴 API Key。
- 项目只读观察 API、viewer/operator 权限分离。浏览器鉴权由 Dots 产品层处理。
- 资源/算力/性能/工具/连接的开放能力目录、租约、证据指标、资源图、exact scope/action 远端授权与审计；provider 声明不等于验证。
- 模型选择能力/路径后的事务型共享容量预留、provider 接收/启动/未知/结算回执；可选独立 provider 运行器承接宿主安装的版本化工具和持久执行 journal。已有跨宿主只读 SHA 工具真实执行验收；这不是自主最佳调度或性能验收。只有受管 Mesh 合同受此管控，不接管原生工具，见 [共享容量与回执合同](docs/MANAGED-ALLOCATIONS.md)。
- 统一的 `mesh(action, arguments)` 额外工具入口；旧 `mesh_*` 保留，连续旧线程不重建。见 [模型入口合同](docs/MESH-GATEWAY.md)。
- 独立节点 authority/worker、断网本地任务、持续 native 子 agent、持久重连退避与去重维护任务。
- 可选的节点→本人文字通知回传：显式授权 `owner.notify`、固定账号/目的地、持久提交与原子去重；不启动第二个微信接收器，不自动转发历史记录或节点任务结果，详见 [通知合同](docs/OWNER-NOTIFICATIONS.md)。
- A2A 远端代理 child 回到原父账本并自动续接；SSH 另有执行 journal，未知结果不重放。当前自有 `mesh-a2a/1`，不冒称标准 A2A 兼容。
- 实验性 native Live 同线程/账户 pin/权限与事件核心；真实连接、音频和常驻接线须另验收。

## 运行与验证

```bash
python3 -m unittest discover -s tests -v
python3 -m assistant_mesh --config /absolute/private/server.json serve
python3 -m assistant_mesh --config /absolute/private/worker.json worker
python3 -m assistant_mesh --config /absolute/private/operator.json status
python3 -m assistant_mesh --config /absolute/private/node.json node
python3 -m assistant_mesh --config /absolute/private/provider-client.json provider --owner-config /absolute/private/provider-owner.json --action serve
```

配置/token 必须是本人持有的私有普通文件，放在仓库外的 `0700` 目录。认证、数据库、聊天、rollout、媒体都不能发布。`scripts/configure.py --help` 与 `deploy/` 提供部署入口。RPC 只监听 loopback，跨设备用 SSH 隧道或明确部署的 TLS 入口。

`auth_home` 只传给专属 Codex 子进程；wrapper 和原始二进制均使用同一个受保护目录。不要改当前会话的全局 HOME/CODEX_HOME。显式 `codex_accounts` 可在启动/认证失败、尚未开始 turn 时使用另一个本人授权账户；不能借此重放已起效 turn。Chat/Live 的主账户 pin 独立于此推理池。

两通道和拓扑见 [NETWORK](docs/NETWORK.md)，节点部署见 [NODE-RUNTIME](docs/NODE-RUNTIME.md)，远端自动续接见 [A2A-DELEGATION](docs/A2A-DELEGATION.md)，Live 边界见 [LIVE-BRIDGE](docs/LIVE-BRIDGE.md)，账号隔离见 [ACCOUNT-POLICY](docs/ACCOUNT-POLICY.md)。

## 控制接口

所有 `/v1/*` 需要 Bearer token。node 身份由凭据绑定，不能从请求体伪造。viewer 不能创建任务、控制执行、批准问题或发通知。

| 接口 | 用途 |
| --- | --- |
| `GET /v1/status` | 脱敏运行状态 |
| `GET /v1/projects` | operator/viewer 按游标观察实际任务，不建另一套面板任务库 |
| `POST /v1/heartbeat`, `/v1/claim`, `/v1/task/update` | 节点声明、领取、fenced 更新 |
| `POST /v1/tasks`, `/v1/task/control` | owner 创建、暂停、恢复 |
| `POST /v1/task/status` | operator/viewer 查看实际结果和原生状态 |
| `POST /v1/agent/action` | 模型记忆、委派、等待、通知 |
| `POST /v1/session` | 同 scope/harness 的选定原生状态同步 |
| `POST /v1/interaction`, `/v1/interaction/resolve` | 问题/单次审批与 owner 回答 |
| `POST /v1/task/steer`, `/v1/steering` | 当前 native turn 注入后续指令 |
| `POST /v1/inbox`, `/v1/notify`, `/v1/notify/status` | 归档、防重通知；accepted 不等于手机确认 |
| `POST /v1/budget/reserve` | 默认零支出预留，不实施采购 |
| `GET /v1/resources`, `/v1/resource`, `/v1/resource/graph`, `/v1/capability-events` | 动态能力、实测指标、图与近期审计 |
| `POST /v1/resource/action` | provider 注册/观测/请求授权，operator 才能审批 |
| `POST /v1/allocation/action` | 认证绑定的共享容量池、预留和 provider 回执；回执不证明工具已执行 |
| `GET /v1/mesh/hello`, `/v1/mesh/links`, `/v1/mesh/task` | 认证节点、链路与原 sender 的任务结果 |
| `POST /v1/mesh/delegate`, `/v1/mesh/send`, `/v1/mesh/report` | 活任务租约下远端子任务、原子防重接收、仅证据报告 |
| `POST /v1/mesh/notify`, `/v1/mesh/notify/status` | 已授权节点向绑定本人回传文字/查询回执，不可指定收件人 |

## 未完成范围

- 已有动态资源图和质量/授权账本；自动最佳调度、逐跳工具调用与实际性能测量仍需证据，不把目录当调度器。
- Native rollout 路径恢复是实验性接口。活跃外部效果中断先核对，不自动重放；reconciliation/release 工作流尚未完成。
- 仍是单一控制账本，没有宣称完全去中心化或消除单点。
- 可验证 Mesh 根身份/撤销同步、外围设备代理迁移与真实共享资源执行调度尚未完成；容量预留和回执不等于实际调度验收。它们已列入核心合同和失败验收，不因能力目录存在而标完成。原生工具强隔离不属于此项目目标。
- Pi/离线模型、技能版本发布回滚、模型主导自维护的真实修复、云端持续调度、远端媒体及跨夜手机收件仍需各自证据。
- Dots fork 上游 browser live view、vault、语音和部分插件 UI 尚未完整接入 native runtime。界面存在不代表能力已接通。

见 [Dots 能力对标](docs/BENCHMARK.md) 与 [实测记录](docs/ACCEPTANCE.md)。
