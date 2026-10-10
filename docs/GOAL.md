# 持续 owner goal

更新时间：2026-10-10。整个 goal **active，未完成**。本文件是后续 owner
指令的有效目标合同，与 [PLAN](PLAN.md)、[TASKS](TASKS.json) 一起续接。
保留原生 goal 创建时间、连续会话、累计使用量和失败身份，不为改措辞
重建 goal。历史 objective 的 OpenClaw/云端独立入口措辞已被 owner
后续要求取代：Dots 对标、Codex/Pi、沟通能力多节点承担。当前控制工具
不能原位修改 native objective；本文件不冒充该操作。

## 体验与架构

通用个人助理：从顶层项目到工具创建、部署、自维护、模型与网络兜底，
由连续 Leader 主动计划、委派、等待、交付和恢复。产品/能力对标 Dots，
参考真实开源实现，但不宣称取得 OpenAI Dots 源码。Codex 原生 harness
优先，Pi 可选；不以 OpenClaw/OpenCode agent 替代 Leader。

**聚合时像一台机器、一个聪明人；分开时多个聪明人各自工作。**
薄的统一 discover/call/delegate/status/artifact 入口，让模型选择
机器、路径、能力和并行工作，不要求本人逐一配置每次底层操作。
不是复制多个账本后宣称全局共识，也不是强迫一切变成 RPC。

原生 Shell、文件、网络、浏览器、MCP 等不被 Mesh 阻断。Mesh 只为额外
共享能力管理身份、归属、容量、期限和外部副作用。日常同 owner 使用
尽量复用既有授权；跨主体、新费用、未决副作用独立核对。失联节点
仍可自主联网、排障、多种方式寻找 Leader，恢复后逐条对账。

## 底层和中层优先

- A2A 与 agent→SSH/原生其他机器互补。独立节点可自主运行，入网后
  暴露 agent/工具/算力/网络能力；临时节点也可接入。
- 以 owner 的 Mesh 聊天与 agent_mesh 文稿中“资源/算力/性能皆能力”
  为中心。记录范围、单位、时效、成本、隐私、共享瓶颈和真实回执，
  不用静态标签/永久模型排行榜代替模型决策。
- VPS 公网出口供本机和其他节点受管 ChatGPT/模型请求优先使用；
  校园内网出口由有连接的节点提供。VPS 不通允许 native 备用联网，
  不以全局代理阻断自主性。网页、API 认证、真实推理分别验收。
- ClawBot 是本人↔Leader 的沟通能力，不是 Leader 工作区，不应永久
  绑定 VPS。本机/VPS/未来节点可承载，接收游标、上下文、原消息/task
  与出站未知状态可安全交接；接管与 Leader 位置分离，不凭 ping
  失败启动重复接收/发送。
- 公开合同环境中立；由节点 agent 按 Windows、手机、Linux、临时
  云环境发现和改装，不要求写完所有平台或照搬 systemd。

## 连续智能与入口

Leader 尽量同一 native thread，使用 Codex 原生压缩/记忆；同类项目
子 agent 尽量复用。保留完整历史、goal/plan/task、真实效果与 unknown
身份，不因换模型/节点重放。跨 harness 无损连续性单独证明，不能
直接把 Codex JSONL 当 Pi session 或用应用短摘要冒充全部记忆。

ChatGPT Chat、Codex、终端、微信都可为入口，有实际执行能力也可为
节点。**OpenAI 官方 Codex Cloud 不是 Alibaba VPS**：内部 node ID
`cloud` 保留，展示称 `ali-vps`；官方临时节点称 `openai-codex-cloud`。
Chat/Live 使用主账号，Codex 使用已授权账户池。Chat 推理、工具、
定时任务可能力化，但不承诺无限额度、任意 API 或常驻存活；Cloud
job 结束后持久状态留在 Mesh。

每 3–5 天检查 OpenAI/Codex、模型、skill/协议与主仓，在既有授权及
零新增支出内隔离验证、可回退升级。模型自行选择当前可用模型。
第三方免费模型/未来 Claude 中转能力不等于改用 OpenCode harness；
协议、认证、成本、数据用途和工具续接需验证。

Live、微信语音→ChatGPT Live、面板/网页、业务为后续 Mesh 委派项目。
面板展示项目/能力/成果但不是一切，不挤占基础能力与自主恢复。

## 当前交付顺序

并行推进 PAM-009 沟通接管、PAM-002/004d 真实通用出口、PAM-010
Codex/Pi 多模型、PAM-006d/e Chat/官方 Cloud。PAM-007 原生 goal
完整生命周期另验，但不能成为全部基础能力等待的串行闸门。
风险相称测试和一次有边界实测后继续，不追求先写完所有环境/错误。
当前没有全局 HA 或整体完成证据，见 [今日报告](STATUS-2026-10-10.md)。

本次续接已实现有限源码：VPS owned SOCKS5h 路径并实测公开 HTTPS；
Codex/Pi 显式 provider/私有配置与连续性保护；ClawBot 只读通道
checkpoint。正式核心未替换，Chat/官方 Cloud、校园出口、ClawBot
自动接管仍未验收。下一片优先出口统一能力/实际承载及沟通失权交接，
保持本 goal active 与原任务身份；具体状态见 PLAN/TASKS，不缩小目标。
冻结d8bfeda双端1707项与公开CI已通过，Codex两版本真实配置读回和
正式VPS只读通道快照已实测；这不是第三方推理或ClawBot接管完成。
随后通用HTTPS callback和单一指定witness的候选/once-only fence源码
已冻结为dd5ae77：同归档双端1761项及公开CI通过。已新增独立VPS
公开HTTPS承载、能力/容量登记和续租，本机实际能力投影可见；两条
公开目标独立GET/TLS/正文SHA核验通过。原Leader通用调用task已经
提交，原task epoch16/原thread已实际选路/预留，独立服务自动GET/
结算，6294字节产物正文SHA与Git oracle相同、容量归还；native
task已同thread自然completed、native plan三步完成，不是整体联网/认证模型/多候选性能验收。
同账号较新native认证已备份同步，零模型account/quota核验通过，
严格旧前缀历史已保留完整备份并同路径原子刷新，权威行未变；
仍不换原task/thread、不清历史或重放旧unknown。ClawBot fence还未
接入真实Channel/API/Store，仍是VPS唯一收发器，不冒称多节点HA。
校园新增本机和VPS经native SSH借本机校内HTTP200证据，未登记通用
校园代理。普通Chat官方插件路线已重新确认，实际HTTP/OAuth/账号
工具与官方Cloud job仍推进，不改成本机VPS冒充。
