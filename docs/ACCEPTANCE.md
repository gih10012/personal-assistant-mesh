# 运行证据与待验收

2026-10-07 至 2026-10-08。公开记录不含账号、私有消息正文、服务器 IP 或凭据。

## 已观察到

- 本地官方 Codex 认证完成真实模型请求，无新 API Key。
- 原生全功能环境实际写文件并读回，不是只给建议的只读 worker。
- Leader 实际创建持久子任务；子任务完成后父任务跨轮次自动恢复并得出正确结果。
- 前文回忆测试禁止文件和 mesh 手工记忆：第一项仅在 native context 记住测试内容；重启自己的 worker 后，新任务准确回忆。它证明该原生 thread 连续，不证明所有 native memories 已后台生成。
- 云端微信持续记录轮询/outbox，受理多条项目通知；每条仍 `delivery_verified=false`，未冒充手机确认。
- 云端 Codex 0.159.2 传输完成，SHA-256 与本机一致，版本执行正常。
- 测试覆盖 scope/harness 隔离、Leader/task fence、未决效果不重放、畸形答案不毒化批次、native plan/goal/steer 请求、归档和发送防重。

## 不可据此推断

- 跨机器 native thread 的实际接续和完整接管。
- 断电跨夜继续工作、手机真实收到主动通知。
- Native memories 后台提取/合并已完成；它有 idle/quota 条件。
- Pi/离线模型已真实可用；适配不是后备服务。
- pause 立即 interrupt 进程：它立即 fence 账本，worker 观察失效后退出。
- 活跃外部效果已核对可恢复：当前 needs_review、不重放，reconciliation/release 仍需实现。
- 云端媒体、browser live view、vault、语音、所有插件、技能回滚、自维护已完成。
- 单权威控制面无单点或已完全去中心化。

后续实测分别记录任务状态、真实输出、作用节点、native 身份、故障/恢复步骤及效果核对。
服务 active 和单元测试不替代端到端验收。

## 2026-10-08：新增组网、资源能力与通信适配

- 本机全套 241 项测试通过（Python 3.14），包括新节点 supervisor、
  A2A 接收事务、防重复交付、确定拒绝与先未知后拒绝的区分、远端子任务
  结算唤醒原父任务，以及资源 API 的身份/权限边界。
- 两个现有本机 profile 分别通过真实 native `account/read` 登录检查。
  单独的通信主账号 pin 与推理账号池已实现；尚未测试额度耗尽后的切换。
- Native Live 模块 35 项测试通过；20 种请求通过当前 0.159.2 生成 schema
  校验。这仅证明适配形状，不证明账号有 Live 权限或音频链路成功。
- 三次原 Leader 合成文本 Live 探测在 `thread/resume` 阶段返回原生错误，
  没有到达 `thread/realtime/start`。没有录音或拨号，也没有以新会话替代
  连续 Leader；失败快照未覆盖原生会话。`excludeTurns=true`（仅省略响应
  UI turns）也未解决跨 profile 的恢复错误；真实 Live 尚未连通。
- Dots fork 的本机生产服务通过匿名隔离、owner/viewer、CSRF、私有 API
  与真实控制面投影检查。它是观察入口，不是节点自治或模型能力的替代。

本节新增网络代码此时仍待实际两机器部署/跨机验收。协议为自有
`mesh-a2a/1`，不宣称与标准 A2A 自动互通。离线模型、跨夜存活、未决
外部效果 reconciliation、常驻原生 Live/完整音频链路仍待实现或验证。

## 2026-10-08：原生自治边界修正

用户随后明确：所有原生功能保持可用，管控只在 Mesh 额外提供的能力入口内。
已撤回整机强制网络 Broker/原生 Shell 隔离的设计和失败验收；原聊天助手的
相反建议不构成本项目需求。离网维护任务明确允许原生联网和多路径重并网，
不把资源注册当作原生工具的前置条件。

统一 `mesh(action, arguments)` 与全部旧工具并存，沿用当前认证 Client 和
任务 fence。新的测试验证开放能力类、结构化授权拒绝、真实 loopback HTTP
远端委派及相同 ID 重复派发；目录和授权不冒充执行 adapter。既有连续
Codex thread 不为了注入新名字而重建。新增私有 Unix Client、stale socket
guard 和 canonical 原生会话恢复测试，不等于跨机器恢复/双向模型验收。

模型 worker/embedded-node 服务模板不再额外设置 `NoNewPrivileges` 或
`PrivateTmp`。这恢复本人账户的正常 OS 权限，并不新授予 sudo 权限或
放宽 Mesh API 身份、预算、防重和作用域规则。部署与原生执行的实际结果
须单独记录，不能只凭模板断言机器已生效。
