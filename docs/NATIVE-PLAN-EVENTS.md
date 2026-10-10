# 原生 plan / goal 事件的观察边界

来源：已安装 Codex 0.162 生成的 app-server schema，以及官方
[app-server](https://learn.chatgpt.com/docs/app-server) 和
[Goals cookbook](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex)。
plan 是当前 native turn 的真实事件，不从输入、协调 JSON、旧 checkpoint
或 `mode=plan` 推导“已经有计划”。本片观察事件并开放原生 plan 工具，
不开启长期 goal。实际 cloud binary 版本与运行期验收另见下文。

`turn/plan/updated` 必须精确匹配当前 `threadId` 与已回执的 `turnId`，
保留原 params、解释与步骤；原生状态为 `pending/inProgress/completed`，
不改成应用自行猜测的状态。旧 turn、其他 thread、缺身份或畸形计划
不能污染当前计划。当前 turn 的 started/completed metadata 同样保留。

RPC 等回复时仍按原顺序 deferred 通知，随后 finish 统一处理；不在 RPC
等待里递归 tick/steer 抢走其他回复。goal updated/cleared 是原线程真实
快照，不自动 create/set/clear 或改预算；它不是模型自主长期目标已验收。

12 项新离线事件 fixtures 与 native protocol/工作合同/额度诊断等相关
69 项回归已通过，未启动模型/认证或写 goal。修正缺少可选 callback 的
原额度失败 fixture 后，本机完整 1279 项通过（145.289 秒）；首轮 1278 项
有一个 callback AttributeError，首轮候选未部署。正式加载另记具体版本，
不将 source fixture 当运行期 native plan 验收。历史 PAM-004c 实际自动
executor trial 已完成，但它的 native plan 为 null；模型报告 update_plan
未提供。该旧观察保留，不因下面新 trial 改写旧计划或借新 Leader 绕过历史。

后续只读核对确认 VPS 实际 binary 为 Codex 0.159.2，两个运行 profile
均无 config.toml。官方 0.159.2 与 0.162 的
`resolve_update_plan_enabled` 都在缺少 `tools.update_plan.enabled=true`
时返回 false，router 只有 enabled 才注册原生 PlanHandler。
[0.159.2 config](https://github.com/openai/codex/blob/ff6aec96948b70d94983af2641a6b67c94faeff5/codex-rs/core/src/config/mod.rs#L2705)，
[0.159.2 registration](https://github.com/openai/codex/blob/ff6aec96948b70d94983af2641a6b67c94faeff5/codex-rs/core/src/tools/spec_plan.rs#L1153)。
本片补子进程 `-c tools.update_plan.enabled=true`，不修改 profile 或全局
配置。owner 可在受保护 worker Codex 配置用 `native_plan_tool:false`
显式关闭；非 bool 拒绝在 auth discovery/spawn 前。它是开放原生工具，
不是伪造事件/计划、权限门禁、新目标或预算。3 项新 child-config fixtures
通过；源码与原线程运行期验收现已完成如下。

## 正式加载与真实原生 plan 验收

两端正式源码为 `3198c877234a8fd0c6912fdbe218f47185effedc`，同一冻结归档
SHA-256 `8362e05085179a06d2c9ff534df72a4460910c4f7ba97f696f2c52bf0b1436e6`。
laptop 完整 1282 项通过（146.487 秒），VPS Python 3.6 完整 1282 项通过
（143.941 秒），[公开 CI](https://github.com/gih10012/personal-assistant-mesh/actions/runs/38020573081)
实际 success。升级仅换源码，配置、原业务/原生会话/历史/执行表保留，
两条 laptop epoch 0 guarded 队列未改；未恢复 DB/history，独立 provider
进程未重启。child 配置未改变模型、预算、全局 profile 或原生权限。

实际 task `PAM-007-native-plan-handoff-20261010-v1` 在 cloud completed
epoch 1，使用同一原 Leader thread `01a116fc-8aae-7001-a0e2-07a1073c5bcb`，
turn 为 `01a123de-2b04-7d30-94b2-2bd8444a4b46`。Root 从实际 authority
只读核对 checkpoint：plan 是真实原生通知，threadId 与 turnId 精确对应
本轮，两个步骤均为 completed；goal 为 null，side_effect_started 为 false，
runtime_failure 为 null。完整结果 SHA-256 为
`7914918df2e4abd37ce1642b637417efa8a2e698d70ae26b543f7e6ad18de48a`。
这证明原线程本轮已能调用原生 update_plan 并保留事件，不证明后续自动
turn、长期 goal、3–5 天 schedule 或模型自主选型已实现/启用。

## 租约绑定的长期 goal 控制器候选

官方 active goal 在 idle 边界可以自动启动后续 turn。正式 `3198c87`
仍是 single-turn adapter，不能直接在 completed Leader 打开长期 goal。
新的源码候选在任何可能启动工作的 thread resume/start、goal set、turn
start/steer 前同步写同 task/epoch intent；read-only 也不等于网络/MCP
零副作用，未知执行标记不按 sandbox 名字省略。这不拦截原生能力。

控制器读原生 actual goal，不从 checkpoint 重设状态/用量。新目标先把
任务输入放进 turn，再 set goal；已有 active goal 不重复 turn/start，
跟踪同 thread 的真实自动 turn 并仅 steer 一次本次输入。每轮的 ID、
计划、goal 与生命周期持续写 checkpoint，旧 plan 不冒充新 turn 计划。
暂停、阻塞、额度和 complete 均保留原生状态，不由模型擅自重新 active。

实际 terminal/cleared goal、当前 turn 终结、原生 idle 和通知排空仅是
quiescent。取得被核对的 rollout 路径后，必须停止/回收运行器并核对
最后通知，才 settled、清效果标记、上传稳定文件并结束 Mesh task。
迟到工作、新 active goal、未回收进程、断线和超时均保留原 ID/unknown；
超时不调用会隐式暂停目标的 interrupt。`waiting_children` 不再由
authority 擅自制造 False。嵌套 RPC 保留外层回复，RPC 等待时仅暂停
可选 steering，heartbeat/fence 和原生工具不因这个 bookkeeping 被禁用。

34 项原生 lifecycle 和 12 项 Worker/Store 新 fixtures 已通过；当前
105 项原生相关测试通过。独立审查发现并复测了旧通知覆盖新快照的
时序漏洞，已修正。它们是离线合同，不是实际长期模型 goal 的验收。
此候选尚未正式加载；正式运行器仍为上述 plan 已实测的 `3198c87`。

以下为明确的后续任务，不宣称完整 HA 或全天候自主已实现：

- active goal 的安全协同 yield 尚未实现。原生当前 turn 结束且模型
  请求等子任务时，候选报告 `codex_goal_coordination_yield_unavailable`，
  停运行器、保留效果/目标状态，不假称 paused/complete 或安全可重放。
- 完整 rollout 目前只在运行器最终停止后上传，途中原节点的原生
  上下文连续，但未实现每 turn 跨节点复制；丢失机器不等于可以拿旧
  artifact 自动接管。后续必须保留原生字节/压缩，不用应用摘要替代。
- RPC 等待期间的强实时 lease watchdog、普通无 goal 单轮的完整进程
  fencing 和真实长期 goal/3–5 天周期检查，仍待各自实际验收。

不为这些缺项 fork 新 Leader、不重放 unknown；原生 Shell、联网、文件、
MCP 和自主排障继续可用。
