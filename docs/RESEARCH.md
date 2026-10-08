# 能力化资源与模型主导的 Agent Mesh

研究日期：2026-10-08。这是设计依据与实验路线，不是完成声明；部署证据另见 [ACCEPTANCE.md](ACCEPTANCE.md)。这里只公开工程结论，不上传用户提供的原始文档、内部项目代码、账号资料或业务数据。

## 1. 从用户材料提炼的核心思想

用户材料把任务、能力与可达性分开：设备是承载体，任务进入持久状态后不依赖发布者在线；迁移的是任务状态和中间结果，不是进程。计算、文件、传感器、网络入口、邻近连接和协议代理都可以成为临时组合的资源。性能也是这种能力的属性，而不是“电脑强、服务器稳定、手机只输入”这样的永久角色。

结合用户后续要求，系统应回答四个实际问题：

1. 模型现在能做什么，以及还能自主发现或创造什么？
2. 哪个节点在当前授权、负载和可达条件下能完成这件事？
3. 模型做出了哪些可复用工具、服务和成果，它们有什么真实证据？
4. 节点、网络或模型失效后，怎样继续任务而不丢记忆或重复外部效果？

材料中的网关限制针对跨主体的远程暴露；不能把它解释为本人的已授权 native 终端也只能执行预设操作。发现目录不是白名单，新增能力类型不应要求修改中央枚举。模型可以继续用终端探索、安装依赖、写程序、配置工具、测试和改进；真实费用、第三人通讯、不可逆变更与权限扩张仍按本人授权处理。

### 能力应是有证据的开放契约

下面是设计维度，不是强制每种资源填满的固化表单：

| 维度 | 要表达什么 | 不应误称为什么 |
| --- | --- | --- |
| 语义 | 能完成的操作、输入输出、适用任务、可扩展元数据 | 固定设备角色或永久工具菜单 |
| 承载与依赖 | 实际执行节点、程序/模型版本、数据位置、依赖服务 | 登记了就代表依赖已安装 |
| 可达性 | 调用路径、方向、网关、中继及路径租约 | 边连接起来就代表真实可达 |
| 当下性能 | 工作负载、时间、单位、样本、延迟/吞吐/成功率、证据来源 | 一个脱离任务的万能性能分数 |
| 可用性 | 声明、健康观测、租约、容量、近期失败 | heartbeat 等于端到端成功 |
| 授权 | 实际调用主体、资源、动作、范围、到期和撤销状态 | 能发现就能使用，或一个 grant 通行全网 |
| 成果 | 来源项目/任务、产物引用、版本、验证命令和结果 | 模型说“做好了”就已经交付 |

例如“推理能力”不只有 GPU 显存：还包括可用模型、输入模态、上下文、启动时间、峰值内存、网络依赖、任务验收成功率和已授权费用。网络路径也有带宽、时延、稳定性和电量成本。只比较 tokens/s 会忽略工具调用成功率与完成质量。

“已声明”“已验证”“当前可用”“对这次调用已授权”是不同事实。验证也必须带范围：通过一次小文件读取不证明能读取所有文件，通过模拟 BLE 不证明真实手机后台或硬件网关可用。

## 2. 参考莫比乌斯的是事实源和边界，不是复制产品

只读研究了本地 Möbius 的工程说明、工作区页面、访问规则与权限测试，没有访问生产 API，也不复制内部实现。可迁移的通用模式是：

- 项目归属用于解释工作；权限是独立边界，不能把业务分类树变成隐式授权树。
- 视图是同一事实源的筛选、分组和布局，不另存一套任务状态，不改变可见性。
- 任务可追踪到输入来源、讨论、成果、测试证据和变更历史；外部系统通过版本化接口与幂等 outbox 连接。
- 管理配置的角色不自动拥有个人任务或所有数据的读取权。详情、搜索、附件、通知与实时订阅都要遵守相同读取边界。
- 跨应用授权保留原始操作者和目标资源身份；应用登录、应用能力、个人数据权限不是同一回事。
- 浏览器只拿自己的会话与被授权数据，不拿云端或模型凭据。审计索引记录主体、操作、资源和结果，私有正文与凭据不进入公开日志。

内部文档与当前实现存在细节差异，因此这里只借鉴上述原则，不声称复制其完整权限语义。个人助手也不应机械套用组织协作平台的“任何可读者都能编辑”规则。

localhost 面板应以“项目 / 活动 / 成果 / AI 新建工具 / 已发现能力”为观察入口。一个项目可以有不同来源的任务、持续 agent、子任务和多个产物；按不同视角显示同一批事实。面板关闭时任务仍继续，面板没有某个按钮也不妨碍模型通过 native 工具完成已授权工作。

## 3. 前沿技术依据与选用理由

本节的官方页面均实际打开或获取全文。协议能力、公开 API 能力与本机 native harness 能力不能互相冒充。

### 3.1 Codex 原生连续性作为主路径

Codex app-server 提供 thread 恢复、原生压缩、goal、运行中 steer、审批及流式事件。实验性 `dynamicTools` 会保存在 rollout 元数据并在恢复时取回；`model/list` 返回当前账号的模型与能力。应以安装版本生成的 schema 验证接口，不把文档最新字段直接强塞给旧 binary。[官方 App Server 文档](https://learn.chatgpt.com/docs/app-server)

工程选择：保留 Leader 的同一 native thread；子 agent 按项目与身份保持连续；账本记任务状态、租约、外部效果与成果索引，不能用应用自写摘要冒充 native compaction。协调工具补充原生终端/MCP，不替换它们。

Codex 的 local memories 与 ChatGPT memory 分开，默认关闭，后台生成受闲置、会话资格和剩余额度影响；开启不等于已经生成。它是跨会话回忆层，不能替代同一 thread 的连续上下文，也不能作为必须执行规则的唯一来源。[官方 Memories 文档](https://learn.chatgpt.com/docs/customization/memories)

### 3.2 动态发现而非把所有工具塞进上下文

OpenAI 的 tool search 支持延迟加载工具定义，也支持由客户端根据项目状态或自己的目录完成搜索。公开 Responses 接口与 native app-server 的接线不同，不能因为 API 有此功能就称 Codex 适配已实现。[官方 Tool search 文档](https://developers.openai.com/api/docs/guides/tools-tool-search)

设计推论：先给模型紧凑的能力概述，让它按意图查找；选中后再获取契约、用法与证据。搜不到现成工具时，模型仍可通过已授权终端发现新入口或创建工具，再把经过验证的契约沉淀。搜索只是帮助模型获取信息，不成为“未搜到便禁止执行”的新围栏。

实验应同时测工具选择正确率、端到端成功率、输入量和延迟。省上下文但漏掉关键工具，不算先进；从目录返回的描述也不能升级成更高优先级指令。

### 3.3 程序式工具编排保留强模型判断

官方 Programmatic Tool Calling 允许模型生成 JavaScript 并行调用、过滤和聚合工具结果；其隔离 V8 自身没有一般文件系统、网络、包安装或 subprocess，外部行为仍由接入的工具执行。[官方 Programmatic Tool Calling 文档](https://developers.openai.com/api/docs/guides/tools-programmatic-tool-calling)

设计推论：可预测的数据合并、批量只读探测和结构校验适合代码编排；语义判断、凭据续接、授权扩张与未知外部效果仍需要明确处理。它不能代替模型的完整 native 终端，也不应强制每个步骤走一套静态工作流。并行度应由当前资源与依赖决定，不是把每项任务固定拆成同样数量的 agent。

### 3.4 MCP 与 A2A 分层接入

MCP 2025-11-25 支持分页发现工具和工具列表变化通知；工具结构化结果可带 schema。工具 annotations 不应从不可信服务直接当成安全事实。[MCP Tools 规范](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)

A2A v1.0.1 的 Agent Card 提供发现信息，可通过鉴权后的 extended card 渐进暴露能力；该版本使用 `capabilities.extendedAgentCard`。服务仍负责每次操作和任务列表的访问控制，卡片不证明服务实际性能或授予调用权限。[A2A v1.0.1 规范](https://a2a-protocol.org/v1.0.1/specification/)

设计推论：先用 native shell/已配置 MCP 验证真实工具，再按需求接入 A2A 的跨运行时任务与成果交换。不要为追求协议名词重新实现已有原生记忆与工具执行，也不要声称采用协议便自动获得去中心化、一致性或资源调度。

MCP HTTP 授权要求校验目标 audience，不把其他服务的 token 当成本站 token，也不做 token passthrough。接入网关时应保留其资源授权边界，而不是将用户的模型 OAuth 分发给任意插件。[MCP Authorization 规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)、[MCP 安全实践](https://modelcontextprotocol.io/docs/2025-11-25/tutorials/security/security_best_practices)

### 3.5 以任务与成果验收工具，而不是工具数量

OpenAI 官方评估路线区分调试 trace 与可重复 dataset/eval：观察工具选择、handoff 和整条流程，再用稳定样本比较变更。[官方 Agent evals 指南](https://developers.openai.com/api/docs/guides/agent-evals)

本项目的设计推论：模型创建工具后，保存源代码/契约引用、版本、适用范围、依赖和验收证据。对可检查的结果优先做实际读取、重放样本或外部状态核对；模型评审补充语义质量，不能代替事实检查。升级后的工具要跑原有样本和失败用例，保留回滚依据。无需为这个流程强制换成收费托管 API。

## 4. 截至研究时的源码状态

下表只表示本次读到的代码与测试范围；并行开发可能继续改变状态。

| 方向 | 已读到的实现 | 仍不能据此宣称 |
| --- | --- | --- |
| 持久任务与委派 | `store.py` 的任务、租约、epoch fence、子任务等待/唤醒、未知发送不重试 | 多副本权威、断网后的全局一致性或任意外部效果可自动接管 |
| Native 连续 thread | `codex.py` + `sessions.py` 的恢复与原生产物存储；协议测试覆盖 resume/plan/goal/steer | 所有跨节点/跨 harness 记忆已无损迁移，或 memories 已完成后台合并 |
| 模型选择 | `model/list` 与 `catalog-first`，未固定一个过时名称 | 目录第一项必然最前沿、最快或最适合任务；完整离线兜底已可用 |
| 开放能力/路径/授权 | 并行新增 `resources.py`：开放 kind/spec、观测来源、租约/epoch、图关系、exact grant、实时撤销检查 | 接线、网关执行和真实网络/硬件已经验收；trusted verifier 的文字 evidence 自动成为物理证明 |
| 项目观察 | `observability.py` 从账本投影，已有 viewer 不能变更任务的测试 | 完整 tool/成果验证索引、所有浏览器安全边界或前端端到端验收已完成 |
| 模型自主造工具 | native 指令与终端能力保留；协调工具不构成操作白名单 | 完整生命周期、自维护、效果核对和无人工干预回滚已闭环 |

当前一个 SQLite authority 仍是控制面单点。资源图的边是声明，不是自动执行的路由。角色/scopes 保护远端 API 与数据可见性；不能把这些控制悄悄复用为本人已授权 native 终端的能力上限。

## 5. 优先实验：从“声明”做到“能做且做成”

这些是待开展或继续补齐的实验，不是固定模型工作流，也不是已完成清单。

### A. 终端能力发现 → 造工具 → 再次复用

给模型一个未预建专用工具的合法任务。允许它读相关说明、发现 CLI/API、检查依赖并编写工具，在本人已授权范围执行。结果要同时包含实际成果和可复用契约；下一项同类任务让持续 agent 自己决定是否复用或改进。

验收：工具确实存在且可调用，输出由独立读取核对；来源、版本和失败行为明确；目录中新增类型不改中央枚举；面板展示新工具和来源任务；未通过验证的声明不显示成“已验证”。不把必须点“发布技能”按钮作为唯一沉淀途径。

### B. 算力/性能/网络共同驱动选择

在已授权节点上测同一工作负载：模型启动、内存、时延、吞吐、工具可用性、结果质量和费用；保存观测时间、样本范围与证据。模型根据任务选择执行位置，解释具体取舍；条件变化时重新发现和规划。

验收：陈旧观测与失效租约不被当成当前能力；高吞吐但任务不合格的节点不会冒充成功；账号可用 model catalog 只用于选择可用候选。默认新增费用为零，测试不隐式采购算力。不能用一次 mock 模型吞吐展示冒充真实离线推理。

### C. 可达性也是可组合能力

先在明确授权目标上验证“本地调用 → 已有云入口 → 受控网关 → 指定资源”的真实路径，测时延和失败情况。未来再接原生 HarmonyOS 短时交互/网关能力；不默认假设 Android route 或手机常驻 worker。

验收：登记/搜索不触发裸扫描或联网；访问保留原始主体与授权范围；撤销、到期、换 epoch 后旧请求失败；网关拒绝越界目的地；中继不会成为无限制代理。真实硬件和 mock 分别记录。

### D. 故障接管与外部效果核对

分别验证未开始任务、只读工作、已完成 native snapshot，以及进行中的外部写操作。节点失效后恢复持续 agent 和目标；发送、付款、发布等已提交却未确认的效果必须先查权威外部状态，不能换节点后盲重放。

验收：新 worker 不能用旧 epoch 写回；Leader 与项目子 agent 的身份及前文延续；副作用可查则核对后继续，不可查则保留明确的 unknown/needs_review。跨夜和实际断电需要独立观察，短暂停止进程不是完整替代。

### E. 安全可观测而非“面板接管一切”

从持久账本显示进行中/已完成项目、native 活动、子任务、成果和模型新增能力，关闭页面不影响任务。以 viewer/operator 验证只读与控制边界，并检查跨来源请求、撤销会话与详情/列表/通知的可见范围。

验收：浏览器无模型/云凭据，private checkpoint 不整包下发，未授权客户端不能 steer/approve/create；产物引用有所有权与验证状态；queued、accepted、已核对送达区别显示。模型仍能用面板之外的已授权 native 工具。

## 6. 持续先进性的规则

先进性不是依赖名或工具数量，而是强模型能否在连续目标下发现、组合、创造和验证能力。

- Codex 优先、Pi 按实际需要兜底；运行时和模型目录动态发现。新增候选先查官方能力，再做代表性任务评估，不盲目全局升级。
- 不将“目录第一项”硬宣称为性能最优；不为了弱环境适配而强迫主模型只能使用最低共同能力。
- 让模型提出新的能力种类、路径和工具。框架约束的是身份、授权、真实费用与外部效果一致性，不是模型可想出的任务形状。
- 工具与结果来自任务事实；代码、契约、性能和验收证据可独立替换。面板是观察窗口，不是定义 AI 行动范围的操作系统。
- 对计划、原型、单元测试、真实运行、交付确认和长期观察分别记证据；保持目标完整，不把最容易通过的局部演示当成通用个人助理已完成。

## 7. 实时语音是通信能力，不是另一套 Leader

本节为 2026-10-08 的只读研究；未开启麦克风、录音、拨号、读取凭据或请求语音模型。

官方产品名为 ChatGPT Voice，由 GPT-Live 支持，在 ChatGPT Desktop 的 Chat/Work/Codex 中进行实时对话和任务协调；Plus 的可用性还受 rollout 与 workspace 影响。已存在任务的语音入口可以使用该任务对话与选定模型，但当前账号、Linux/Firefox 界面是否提供该入口尚未证实。[ChatGPT Voice 产品说明](https://learn.chatgpt.com/docs/features/voice)

官方 GPT-Live 架构将实时说话/倾听与 backend 的推理、工具、长任务分开，client delegation 可以连接已有 agent harness。这个分工适合本项目：ClawBot、网页或实时通话只负责通信，持续 Leader 与项目 agent 仍是工作与记忆主体。[GPT-Live 指南](https://developers.openai.com/api/docs/guides/live)

公开 GPT-Live API quickstart 要求项目 API Key，不能把它当作 Plus 订阅已自动提供的集成。Desktop Voice 消耗现有 Codex 使用预算，额外 credit 与 backend 工作有各自计量；本项目仍禁止未经授权的新费用。[Voice 计量说明](https://learn.chatgpt.com/docs/pricing#how-much-does-voice-cost)

### 本机已存在的实验性协议

只读验证本机 native binary 为 Codex 0.159.2，并读取由该版本生成的 schema。以下方法全部标为 EXPERIMENTAL，方法存在不证明账号可用、连接成功或音频往返成功：

| 方法 | 本机 schema 的必填参数 |
| --- | --- |
| `thread/realtime/start` | `threadId`、`outputModality`：`text` 或 `audio` |
| `thread/realtime/appendAudio` | `threadId`、`audio`，其中必填 `data`、`numChannels`、`sampleRate` |
| `thread/realtime/appendText` | `threadId`、`text`；可选 `role`，默认 `user` |
| `thread/realtime/appendSpeech` | `threadId`、`text`，表示供实时会话说出的文本 |
| `thread/realtime/stop` | `threadId` |
| `thread/realtime/listVoices` | 无必填字段 |

`start.transport` 可以省略，或选 `websocket`、带 SDP 的 `webrtc`、带 `callId` 的 `existingCall`。支持的 schema 枚举含 `v1/v2/v3`；可选 `model` 只是 realtime model override，不应硬填公开 API 模型名称。`includeStartupContext` 省略时包含 native startup context；V3 还支持初始角色文本与 backend response handoff 设置。

`start` 返回空对象形状；必须继续观察 `thread/realtime/started`、`error`、`closed`、transcript 与 `outputAudio/delta` 事件。不能把 JSON-RPC 请求受理当成 Live 音频已可用。`audio.data` 的编码没有在此 JSON schema 中明确说明，不能直接将公开 API 的音频编码约定当成已验证的 native 契约。

官方配置说明区分 CLI `/voice` 的 `features.realtime_conversation` 与 Desktop/app-server voice；设置开关不绕过客户端或 rollout 检查。[配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)

### 后续验收顺序

先保持同一 native app-server 和已加载的 Leader thread，以不录音的元数据/文本实验确认账号与协议响应；随后由本人主动开启的音频输入验证说话、倾听、打断和同一 Leader 的任务委派。当前按任务启动后退出 app-server 的 worker 生命周期需要处理，否则实时会话可能随任务结束而消失。另起会话再复制摘要，不算原生 thread 共享。

以后接本人微信与企微之间的通话，应作为独立音频适配能力：上行仅取指定通话的已授权音轨，下行只回送指定会话，防止扬声器回环，明确停止与失效行为，不默认为全系统录音。公开 API 有音频连接文档，不证明微信/企微通话已接通，也不授权本阶段自动拨号。

## 8. GitHub 实读：Live 接入组件，而非更换 Leader

2026-10-08 只读检索并通过 GitHub contents API 阅读以下仓库的 README、许可证和实际实现；日期为检索时默认分支 HEAD 的 commit 时间（UTC），不是仓库更新时间或已实测日期。没有安装、运行音频、读取本机凭据或访问账号语音接口。

| 候选与固定版本 | 代码实际提供什么 | 可复用部分及主要缺口 |
| --- | --- | --- |
| [Oh My Pi](https://github.com/can1357/oh-my-pi/tree/40e9368ef0458fd9073329cdff4174895f91bc6b)，MIT；2026-10-08；`40e9368ef045` | GPT-Live 的 controller 绑定现有 `AgentSession`；RPC 暴露语音状态/转写/终止；Rust 音频及 WebRTC | 最贴合 Pi 偏好。参考 MIT protocol/controller/RPC 生命周期；不是官方 app-server。Linux 默认 PulseAudio/ALSA 设备，需补指定通话音轨路由。 |
| [hermes-talk](https://github.com/TheSmokeDev/hermes-talk/tree/2dcf20125dae44b04904d0785c8f6d3db55bbcf1)，MIT；2026-10-06；`2dcf20125dae` | 分开的订阅/API Live transport、24kHz PCM ↔ WebRTC、捕获片段账本、任务归属和结果播报状态 | 参考音频队列与幂等/结果展示契约，不安装 Hermes 工作区。订阅 wire 明确来自 OpenClaw MIT，必须保留第三方声明；不能隐瞒来源或据此替换 Leader。 |
| [opencode-gpt-live](https://github.com/malhashemi/opencode-gpt-live/tree/c23101cfdeb3feaf03f09aa678135ca01d72ed4e)，MIT；2026-10-07；`c23101cfdeb3` | Rust WebRTC/Opus、真实回放参考的本地 AEC；凭据留在 server；独立 voice session 转发主 session | 只考虑拆 `native/src/audio/*`、`transport.rs`。不引入 OpenCode harness 或第二决策 agent；默认通话日志、其他应用 ducking 和 helper 自动下载不应照搬。 |
| [meetron](https://github.com/bb8ad8/meetron/tree/87216585e40b55c70d5928d1cb6d11f71f4fd997)，GPL-3.0；2026-08-27；`87216585e40b` | 既有 ChatGPT Web Voice ↔ Meet/Zoom，macOS 双虚拟音频设备、专用 Chrome/CDP、路由验收 | 最接近“不碰账号 token、接既有客户端”的声卡桥思路，但不是 Linux/Firefox 实现；默认每次创建新 Project chat，不共享持续 Codex Leader。GPL 衍生物不能换 MIT 标签。 |
| [brokk-codex-acp](https://github.com/BrokkAi/brokk-codex-acp/tree/fc731af91803aa1664257b879016d1f680ba7896)，GPL-3.0-or-later；2026-07-12；`fc731af91803` | `/realtime` 转发 native `thread/realtime/*`；同一 Codex thread 和权限请求由 app-server 管理 | 可研究 native 映射/事件兼容性，但 README 明确 ACP v1 没有原生实时音频播放，实际代码只输出音频摘要；不是完整语音客户端。ACP 也不是 A2A。 |
| [ChatGPT-persona](https://github.com/harmony365/ChatGPT-persona/tree/cf27d12dfe4993c231d4ce601da8c2becc11a4bf)，源码 MIT；2026-08-01；`cf27d12dfe49` | Linux 按播放进程发现 PipeWire 节点、用 `pw-record --target object.serial` 取 PCM，计算 RMS 活动 | 可复用节点发现/断开清理思路。它不录麦克风、不发语音、不联网传音频，不能当完整通话桥；角色媒体另有许可证，不复制素材。 |

### 两条订阅相关路线必须分清

**A. 原生 Codex / Pi 的 Live 通信接口。** Oh My Pi、hermes-talk 与 OpenCode 插件实码均将订阅 SDP POST 到 `chatgpt.com/backend-api/codex/realtime/calls`，再连 `api.openai.com/v1/live/<callId>` sideband；模型标识是 `gpt-live-1-codex`。这是复用 host OAuth 的第三方兼容路径，不是公开 API Key quickstart，也不是官方保证每个 Plus 账号可用的 API。hermes-talk 的 Live 配置显式分开 subscription/API，不自动付费兜底；其旧 Realtime auth 文件头部注释与当前只读 Codex auth fallback 实现还有出入，接线应以实际分支代码为准。[Pi transport](https://github.com/can1357/oh-my-pi/blob/40e9368ef0458fd9073329cdff4174895f91bc6b/packages/coding-agent/src/live/transport.ts)、[Hermes Live 配置](https://github.com/TheSmokeDev/hermes-talk/blob/2dcf20125dae44b04904d0785c8f6d3db55bbcf1/talk_live_config.py)、[OpenCode wire](https://github.com/malhashemi/opencode-gpt-live/blob/c23101cfdeb3feaf03f09aa678135ca01d72ed4e/src/server/live.ts)

Pi transport 写入 `Codex Desktop` 的 User-Agent/originator，macOS arm64 还可生成 DeviceCheck attestation；不能将其第三方请求标识说成官方支持。优先让已安装的官方 native app-server 自己管理登录/连接，借用 protocol 和音频生命周期，不默认移植 token 读取、刷新或内部信令。[Pi attestation 实现](https://github.com/can1357/oh-my-pi/blob/40e9368ef0458fd9073329cdff4174895f91bc6b/packages/coding-agent/src/live/attestation.ts)

**B. 已有 ChatGPT 客户端 + 虚拟音频。** meetron 所读的 Live 启动路径不调用模型 API，而是专用已登录 Chrome 的 Web Voice UI 自动化。`prepare-chatgpt-live.mjs` 在页面中固定 getUserMedia 的 exact device、音频输出 sink，并检查是否有声音漏到其他输出。它仍把声音交给 ChatGPT 云端、受客户端/账号使用限制；“无需 API Key”不等于“零费用/无限额度”。不能把同一 ChatGPT Project 的新 chat 冒充已有 Codex thread。Linux 实现应独立适配 PipeWire 和实际客户端，不能执行其 macOS 驱动安装器或照搬其 Chrome 麦克风自动授权。[meetron 路由与验收代码](https://github.com/bb8ad8/meetron/blob/87216585e40b55c70d5928d1cb6d11f71f4fd997/scripts/prepare-chatgpt-live.mjs)

公开 GPT-Live/Realtime API 仍是另一条计费产品路线，只有用户明确选择后才可引入；第三方仓库中的订阅 claim 不是本机账号连通证据。[官方 GPT-Live 接入](https://developers.openai.com/api/docs/guides/live)

### 对本项目最值得借鉴的代码关系

- **持续 Leader：** Pi 的 `LiveSessionController` 持有已有 `AgentSession`，收到 delegation 后送 custom message 触发原会话，再将原会话进度/最终可见文本送回该 delegation；不是新建工作 agent。当前只有一个 `activeDelegationId`，重叠委派需要 correlation/队列测试，不能直接假设天然并发安全。[Pi controller](https://github.com/can1357/oh-my-pi/blob/40e9368ef0458fd9073329cdff4174895f91bc6b/packages/coding-agent/src/live/controller.ts)
- **通信生命周期：** Pi RPC bridge 对连接中/活动/关闭持有同一 slot，`live_end` 后不再发 level；mute/stop 清理独立于长任务执行。该接口不提供指定微信音轨选择，不能把 RPC 可用说成通话已接通。[Pi RPC bridge](https://github.com/can1357/oh-my-pi/blob/40e9368ef0458fd9073329cdff4174895f91bc6b/packages/coding-agent/src/modes/rpc/rpc-live.ts)
- **结果的真实状态：** Hermes ledger 以 owner/session/片段标识去重，区分已有 recipient 与新 worker，并分开 result ready、context submitted、播放完成/未知。可借鉴这种证据模型；不照搬其额外 PluginLlm router，不让转写/模型自产 delegation 等同用户新授权。[Hermes coordinator](https://github.com/TheSmokeDev/hermes-talk/blob/2dcf20125dae44b04904d0785c8f6d3db55bbcf1/talk_live_coordinator.py)
- **精准 Linux 音轨：** Persona 发现 `Stream/Output/Audio`，使用 `object.serial` 绑定 `pw-record`，仅当前子进程的数据可交付；节点消失即 detach。现代码只取 16kHz 双声道 PCM 算 RMS，仍需构造有租约/来源/会话绑定的双向传输；节点名称是匹配线索，不是权限证明。[PipeWire listener](https://github.com/harmony365/ChatGPT-persona/blob/cf27d12dfe4993c231d4ce601da8c2becc11a4bf/electron/linux-pipewire-listener.cjs)
- **本地 DSP 可选组件：** OpenCode helper 用实际播放的 reference 执行 AEC，48kHz 处理、20ms Opus 帧，经 stdio JSON 接信令；不接触账号凭据。但它默认设备、通话文件日志与全应用 ducking 不适合直接接指定通话。checksum 验证来自同一 release，只验证下载一致性，不等于独立发布者身份验证。[音频 engine](https://github.com/malhashemi/opencode-gpt-live/blob/c23101cfdeb3feaf03f09aa678135ca01d72ed4e/native/src/audio/engine.rs)、[helper 下载](https://github.com/malhashemi/opencode-gpt-live/blob/c23101cfdeb3feaf03f09aa678135ca01d72ed4e/src/tui/helper.ts)

建议优先级为：官方 native 同一 thread → Pi 的协议/生命周期参考 → 独立 PipeWire 指定音轨桥；只有 native 账号入口不可用且本人明确选择已有客户端时，才走 UI/声卡桥路线。GPL 项目可独立 fork 保留原授权，也可仅研究其架构后独立实现；MIT 模块移植需保留各模块版权与依赖通知。本阶段只是研究，未复制这些模块、未 fork 外部仓库、未完成真实语音验收。

终端远程工作应另外区分 **agent 对 agent 的 A2A 协议**与 **agent 通过 SSH 控制另一主机**；实时语音、ACP、MCP、虚拟声卡都不自动成为 A2A，也不自动授权远端执行。这些通信入口都只能把请求送到绑定的持续 Leader，由其当前工具/权限处理。
