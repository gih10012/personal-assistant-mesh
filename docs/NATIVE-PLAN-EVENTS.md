# 原生 plan / goal 事件的观察边界

来源：已安装 Codex 0.162 生成的 app-server schema，以及官方
[app-server](https://learn.chatgpt.com/docs/app-server) 和
[Goals cookbook](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex)。
plan 是当前 native turn 的真实事件，不从输入、协调 JSON、旧 checkpoint
或 `mode=plan` 推导“已经有计划”。本片只是事件观察，不开启长期 goal。

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
不将 source fixture 当运行期 native plan 验收。PAM-004c 实际自动 executor
trial 已完成，但 native plan 仍 null；模型报告 update_plan 未提供，尚须
实际运行器/工具配置核对，不伪造 plan 事件或借新 Leader 绕过旧历史。

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
通过；实际部署与原线程调用后的 checkpoint 仍需独立验收。

## 长期 goal 的下一片

官方 active goal 在 idle 边界可以自动启动后续 turn。当前 single-turn
adapter 只等待最初 turn，因此仍不能直接在 completed Leader 打开长期
goal。完整方案需在任何可能触发 native 工作前保留同 task/epoch intent
与效果标记，跟踪原 thread 自动 turn、plan 和 actual goal 生命周期并
维持 Mesh lease；只有真正静止/已终结或明确协调 yield 后才能收尾。

不机械从 context/checkpoint 重写 active，不在限额/暂停/阻塞后擅自恢复，
不为 fork-only 的 defer flag 换 Leader。计划 capture 小片不改变现有
goal/set 顺序，不解决这些生命周期风险；实际长期 goal 与周期检查仍
未启用。原生 Shell、联网、文件、MCP 和自主排障均不受此观察层拦截。
