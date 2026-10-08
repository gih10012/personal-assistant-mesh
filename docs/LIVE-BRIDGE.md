# Live 是通信前端，Leader 是持续工作与记忆主体

本模块是可以注入传输层的 native 协议核心，不是“语音已接通”的完成声明。没有启动麦克风、录音、扬声器、拨号、微信/企微通话、浏览器 Voice 或公开付费 API。真实连接与音频验收必须另有现场证据。

ChatGPT Voice 由 GPT-Live 支持；已有 Codex 任务在功能可用时可以直接使用该任务的对话与所选模型。Plus 还受 rollout、客户端和 workspace 条件影响，不能用订阅类型自动推断本机可用。[官方 Voice 说明](https://learn.chatgpt.com/docs/features/voice)

GPT-Live 的实时交谈与 backend 的推理/工具工作是两部分。这个分工用于本项目时，ClawBot、Chat、Live、终端都可以是通信入口；它们不会成为 Leader 工作区或替换持续 Leader。公开 Live API 需要项目 API Key，并按时长及 backend 工作计费；这里没有实现那条路线，不自动将不可用订阅转换成付费调用。[官方 Live 指南](https://developers.openai.com/api/docs/guides/live)

## 固定通信账户，不固定推理能力

本人的 Chat/Live 固定使用已选择的 `@163` 主账号；Codex 推理可以有另行配置和验证的账户池。两者不能混为一个自动切换器。

`CodexLiveTransport` 要求传入 `expected_auth_home`，并核对已加载 native agent 的 `auth_home`。部署方将它指向专门的主账号授权目录，独立通过 native `account/read` 检查当前账户元数据；本模块不会读取或复制 `auth.json`，也不会设置全局 `CODEX_HOME`。目录固定是路由约束，不是凭据内容或账号可用性的证明。当前 app-server 的 auth 类型必须是 `chatgpt`；API-key 认证会被拒绝。失效时报告错误，不选另一账号、不创建新 key、不购买额度。

如果原持续 Leader 所在 native 进程使用另一个推理账户，不能为了过测试另建一个 Live Leader 再复制摘要。部署方必须协调安全的同线程移交、原始 native rollout 恢复及账户归属；未经证明不得称上下文已共享。

## 实现范围

`assistant_mesh/live.py` 使用安装版本 **Codex 0.159.2** 生成的 EXPERIMENTAL schema，与公开 Live API 的 JSON 事件不混用。[官方 app-server 生命周期](https://developers.openai.com/codex/app-server)

| 接口 | 发送到已有 native thread 的参数 | 返回意味着什么 |
| --- | --- | --- |
| `start()` | `threadId`, `outputModality`, `realtimeSessionId`；可选 native `version/model/voice/transport` | RPC 受理，不是接通 |
| `append_text(text, message_id)` | `threadId`, `text`，保持默认 `user` role | 输入受理，不是完成或转写结果 |
| `append_audio(audio)` | `threadId`, `audio`；其中 `data/numChannels/sampleRate` 必填 | 音频块受理，不是识别或送达 |
| `append_speech(text, handoff_id)` | `threadId`, `text` | 结果文本受理，不是已播放 |
| `step()` / `wait_started()` | 消费已绑定 thread 的原生通知 | 分别记录 startup、transcript、输出音频和关闭事实 |
| `stop()` | `threadId` | 停止请求受理；必须另见 `closed` |

没有 `thread/start`、`turn/start`、`thread/resume`、新 executor 或自写记忆摘要。本模块依附已经加载的 native thread；native 后端自行使用它原有的权限、工具、委派和自动 response handoff。没有实现项目 API key、内部 OAuth signaling、仿冒 Desktop User-Agent 或第三方认证刷新。

默认不设置 realtime `model`，更不会把公开 API 的 `gpt-live-1` 名称硬填到订阅协议。`transport` 可省略或使用 native websocket/WebRTC/既有 call 形状；本模块不生成 SDP、不创建内部 call。已有 call 必须另获 `attach_call` 授权且与绑定的 call identity 完全相同。

schema 只把 `audio.data` 描述为字符串，没有证明是某种 PCM/base64/Opus 编码。核心只转发适配层提供的 opaque 字符串。声卡桥应先核验安装版本实际音频格式，不能照搬公开 API 的采样格式。本模块额外拒绝零采样率/零声道与 bool 充当整数，因为这种数据不能代表可播放的有效音频。

## 同一事实源与生命周期边界

调用方必须持有已有 scope 的独占租约，并让 Live 与 native turn 使用同一事件消费器；不能一边 `Codex.finish()/event()` 一边开启另一个消费者。`CodexLiveTransport.poll()` 会从 `deferred` 取 Live 事件，保留其他原生 turn/tool 事件及其他 thread 的原顺序；RPC 之前收到的通知也不会丢失。它不会关闭 Leader 的 native 进程。

当前 worker 按任务退出 app-server，不能直接挂接常驻音频。生产接线仍需由主流程改为租约持有期间的持久 native runtime，或协调安全恢复；“拿到一个对象就能常驻”不是现状。必须保留同一 Leader thread、native compaction、持续子 agent 和持久 task ledger，而非第二套语音任务数据库。

状态是 `idle → starting → started → exchanging → stopping → closed`；拒绝、未知连接和失去租约分别记录 `error/unknown/detached`。这不是自动重连工作流。`started` 是 native startup accepted，不是音频往返。观察超时继续保留同一次请求；未知提交不自动重发。关闭通知才是 native transport closed，输入/输出计数不是完整通话或扬声器播放确认。

一个 native 进程只能绑定一个 Live adapter。0.159.2 的部分 flat 通知没有 session id，无法可靠区分同 thread 上重叠旧连接；不能在未知状态直接换 slot 或 start 重试。后续连接须先核对关闭与队列边界，必要时安全重建官方 app-server 并恢复**同一 native thread**，不能改变 Leader 身份。此版本没有完成跨进程 Live 请求的持久恢复。

## 身份、授权与关联

binding 由可信接入层提供固定 `thread_id/scope/principal/source/peer`，可选 exact `call_id`。入站文本、转写、原始 `itemAdded`、模型生成的 delegation 或名字匹配都不能升级主体、变更 thread 或新增执行授权。binding 与 callback 参数是副本，不受客户端字段原地篡改。

`authorize(binding, action)` 与 `lease_check(binding)` 必须返回 **literal `True`**；缺省拒绝，truthy 的 `{allowed: false}` 不是授权。能力分别为 `connect/send_text/send_audio/receive_audio/read_transcript/speak_result/attach_call/receive_signaling`。每次传送和接收再次核对权限，撤销后不继续将音频转发给 sink。WebRTC 的 SDP answer 也只交给明确获授权的 signaling sink，不写进 snapshot。部署方接既有 ResourceRegistry 时必须取实际 `allowed` 值，检查原主体、动作、范围、到期、provider epoch 和撤销状态，而非让 raw grant object 通行。

失去原 scope 租约时只 detach 本地通信，不再发 `stop(threadId)`，避免结束已经由接管者拥有的线程。租约检查本身不是 native 分布式 fence：部署方仍要保证旧 app-server/节点被可靠隔离，不能宣称本模块已经消除了双写或网络分区问题。

每条外部文本使用独立 `message_id`，每个结果使用独立 `handoff_id`；同标识同内容返回已有状态，不重发；同标识不同内容报冲突。不能用单一全局 `activeDelegationId` 把并行结果关联到最后一个请求。原生 canonical item 按 `realtimeSessionId + item.id` 跟踪，跨 item 交错 delta 不串写，完成重放去重。

`appendText/appendSpeech` 的 native wire 没有这些外部关联标识，flat transcript 也没有输入请求 id。因此外部 id 只证明本模块的一次提交，不能捏造它与某个 flat transcript 的因果关系；native 自动 handoff 的关联由 native runtime 负责。模块的 submission map 是内存索引，重启后必须与持久账本/外部效果核对，不能声称 exactly-once。

## 未来本人微信 → 企微通话

个人微信到本人北方工业大学企微的确切 owner pair 是已指定的未来通信目标。此版本没有拨号、识别联系人、开启麦克风、创建虚拟声卡或转发通话。调用名称相同不证明 peer 身份，也不授权自动接听任何来电。

音频适配层需要把真实 call/session identity、已验证 peer、PipeWire `object.serial` 与授权绑定；只取该通话的上行轨道，只回送该通话的下行轨道，避免全系统监听和回环。节点消失、通话结束、权限撤销或原租约失效就 detach/清理本适配层所建连接，不改其他应用默认设备、不关本人其他登录态。转写/音频默认没有文件记录或公共日志。

`on_event/on_audio` 是 opt-in sink；snapshot 只有标识、状态、计数和安全错误码，没有对话、音频、SDP 或原始服务错误。sink 调用出错记 `sink_delivery_unknown`，不声称播放成功，不自动重放。最终音频交付仍需要适配层的实际路由/播放证据。

## GitHub 参考与移植边界

实现前实际阅读了 [Oh My Pi controller](https://github.com/can1357/oh-my-pi/blob/40e9368ef0458fd9073329cdff4174895f91bc6b/packages/coding-agent/src/live/controller.ts) 和 [RPC bridge](https://github.com/can1357/oh-my-pi/blob/40e9368ef0458fd9073329cdff4174895f91bc6b/packages/coding-agent/src/modes/rpc/rpc-live.ts)，固定 SHA 为 `40e9368ef0458fd9073329cdff4174895f91bc6b`。借鉴“已有 AgentSession 是工作主体”“连接/结束持同一 slot”“停止通信不重建工作 agent”的分工；避免其单个 active delegation id 对并发的限制。

这是按 native Codex schema **独立实现**的协议核心，没有复制上述 TypeScript、内部订阅 signaling、音频库或它的默认设备/录音行为，不声称 fork 了完整语音客户端。后续实际移植 MIT 模块必须保留原版权、LICENSE/NOTICE 及依赖通知；GPL 项目不能简单贴成本仓库 MIT。其他已实读候选、固定版本与 Linux 指定音轨思路见 [RESEARCH.md](RESEARCH.md#8-github-实读live-接入组件而非更换-leader)。

## 验证与仍需证明的事实

运行 `python -m unittest discover -s tests -p test_live.py -v`。测试只注入合成协议事件，覆盖同线程/账户 pin、保留 native 事件、默认拒绝/撤销、会话/item/结果关联、重复/未知提交、关闭边界和安全日志。它不是订阅连接、音频格式、麦克风/扬声器、微信/企微、长期运行或实际断网恢复证明。

真实验收顺序：先在独占 scope 与固定通信主账号下用合成文本观察 startup/error/closed 和能够核对的回答；再由本人主动授权的指定音频源做双向、打断、委派/结果回传；再在确切本人通话中验证轨道、清理和权限撤销。测试或一次短连接不能替代长期观察，也不能把未实现的声卡桥记为可调用能力。
