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

Pi 扩展已移除全局 `tool_call` Mesh 租约检查，额外 Mesh 工具自行检查
fence，统一入口及旧工具保留。14 项专属测试运行实际 TypeScript 源码与
真实鉴权 HTTP，覆盖 Mesh 失联/403/stale409 时原生模拟事件路径不访问
Mesh。SDK 注册使用 shim，原生路径为事件管道模拟；不冒称安装的 Pi SDK、
真实 Pi 模型或离线推理已验收。

包含以上修正的本机完整测试为 336 项通过（Python 3.14，Node TypeScript
合同测试实际执行），不是仅跑定向测试。28 个 Python 源文件通过 3.6
语法检查；云端实际运行及模型任务仍须部署后分别验证。

随后在云端隔离 release 目录发现并修复旧 Python 的 bodyless HTTPError
关闭兼容问题及 mock 参数读取兼容问题；CLI 路径测试不再依赖仓库目录名。
修复后的完整 337 项测试在本机 Python 3.14 和云端 Python 3.6.8 均通过，
含上述 14 项实际 TypeScript/HTTP 合同测试。CI 的慢启动 fixture 给予 10 秒
启动时间，不改变认证、lease、执行或防重断言；新 CI 结果另行核对。

## 2026-10-08：两机上线与实际 Shell 故障修复

- 云端 authority、原生 worker、独立 A2A companion 已运行；本机独立节点、
  原生 worker 与 SSH 通道已运行。云端访问本机的 reverse endpoint 实测是
  本人持有的 0600 Unix socket，不是公网 TCP 端口；双向鉴权 hello 和 link
  检查通过。本机原有工作终端未关闭。
- 双方向各一条实际远端 `remember` 任务 completed，返回指定 ACK；相同
  消息 ID/body 重放仍绑定同一接收任务。这证明真实推理和接收端防重，不
  证明线程跨机器迁移或原生工具完整。
- 节点重启后的 `recall` 确实恢复原 thread，但原生 turn 因配额耗尽失败。
  本机指定失败轮的只读 `thread/turns/list` 与原生终态记录均确认
  `usageLimitExceeded`，没有记录工具调用。本次 recall **尚未通过**，
  不能以原 ID 找到就冒充连续记忆验收成功。
- 云端先前只复制 standalone Codex：版本和文字推理正常，Shell 执行却在
  缺少 `codex-code-mode-host` 时失败。补入官方 **同版本** 0.159.2 musl
  helper，校验官方整个 helper artifact SHA256，再核对解压 binary；没有
  混入本机其它版本，也没有新增原生 Shell 白名单。
- 本机和云端分别执行真实原生 Shell 哈希挑战：预期 digest 不交给模型，
  核对同一原生工具调用的实际输出与最终答案，两端都通过。云端第二次
  通过的 probe 在启动前跳过额度满的第一 profile，用本人已授权第二
  profile；该云端 profile 的旧登录态曾返回 routing discovery 401，
  复用现有授权登录态并保留旧认证备份后实际成功。以上是独立原生 probe，
  尚不替代 systemd worker 内完整工具链/所有 native 功能的验收。
- 源码新增完整官方 package 安装/维护期校验和原生 Shell challenge。
  完整 package 安装器有 13 项离线合同测试；此次云端即时修复的是匹配
  helper，完整资源包的真实安装仍待下载、校验与执行，不能混为已完成。
- `b5b953a` 的本机 Python 3.14、云端 Python 3.6.8 完整 372 项测试通过，
  GitHub 对该 commit 的 Python 3.8/3.12/3.14 CI 成功。

可选目录与 steering query/ACK 降级，保留未知结果而不重复 native steer。
额外 Mesh action 的网络/服务错误按未知结果返回，原生 turn 可继续；真正
当前 task fence 失效仍结束该受管任务。推理账户池新增原生配额预检，只在
尚未开始 turn 时选择下一授权 profile；已开始的 task 不盲目重放。Chat/Live
通信主账号不因此切换，也不自动购买 credits 或消耗 quota reset。

## 2026-10-08：原任务连续记忆恢复与维护能力

- 上节两条 `recall` 随后均实际 completed，输出与之前记住的私有随机
  nonce 完全一致。逐端核对本地权威账本：原任务 ID、原 native thread ID
  保持不变；此前的节点/worker 重启不被替换成新会话。启动前跳过额度满
  的 profile，在同一节点的第二授权推理 profile 续接完整原生历史后通过。
  相同消息 ID/body 查询重放仍绑定同一接收任务。
- 这两条额度失败没有直接清除 fence 或新建任务。维护脚本先只读核对
  指定原生 turn 的明确 quota 终态、完整日志身份和零执行记录；显式 apply
  再以 epoch/完整前态 CAS 保存原任务及会话的审计备份，传输完整原生
  历史（保留失败轮及旧 chunks），释放同一任务。它只处理已证明没有
  执行的 quota 失败；未知效果、工具执行、未知日志结构和变化中的任务
  一律不适用，不代表通用外部效果 reconciliation 已完成。
- 云端第二 profile 的既有 session 子目录含 group-write，导致 Mesh 的
  可信恢复入口在模型启动前拒绝。仅收紧四个本人 Codex session 目录为
  0700 后，原任务的既有 waiting_backend 重试完成；未更改文件内容、
  未放宽恢复检查，也没有阻断 owner 的原生 Shell/联网能力。
- 本节证明的是两个节点各自的连续线程在重启及推理 profile 迁移后续接，
  **不是**一条 Global Leader thread 在不同机器间实际迁移接管，也不证明
  native memories 后台提取、Pi/离线模型或完整 Native Live 已工作。
- 可选并行 artifact transport 有 15 项实际 loopback 测试：严格 Range/
  Content-Range、完整 SHA256、0600 原子发布、正确既有文件幂等及错误
  内容不覆盖。它是额外下载能力，不是原生网络策略、安装器或凭据渠道；
  官方完整包的实际下载/安装仍需单独核对。
- 加入维护审计及 artifact transport 后，本机 Python 3.14 与云端
  Python 3.6.8 均完整通过 420 项测试。云端默认 umask 下的维护测试通过，
  夹具显式建立可信目录，生产权限规则未被削弱。

随后补入可选本节点 `runtime_diagnose` / `runtime_doctor`，并强化 canonical
包布局和目录权限校验。本机完整 445 项测试通过，其中 22 项诊断测试验证
只读、凭据/私有正文不输出、未知布局不判坏、Mesh 失联时无需 heartbeat、
调用者不能指定其它 config/path。诊断没有接管原生工具；当前仅是观察和
推荐维护句柄，**不是自动自修闭环**。云端本次新增测试与实际完整包安装
将在完成后另记，不能将本机测试算作云端验收。
