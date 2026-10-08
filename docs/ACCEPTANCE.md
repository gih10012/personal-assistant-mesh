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

云端随后发现 Python 3.6 的递归 mkdir 在默认 umask 下给中间目录增加了
group-write。安装器改为逐层显式建目录，维护/探针夹具亦逐层 0700；原有
可信路径规则未放宽。增加显式只读分段续传、无凭据单次代理和常驻 Worker
Shell 探针后，本机完整 479 项测试通过。其中 transport 的 33 项覆盖实际
loopback forward proxy、环境 bypass 不参与、resume/redirect/whole SHA256；
Worker 探针 15 项通过，使用离线 backend fixture 与真实鉴权 HTTP，不将
这些 fixture 当作常驻模型执行已经通过。相同源码在云端 Python 3.6.8 完整
479 项亦通过。真实完整包和部署者核对的 worker
进程内挑战结果继续另记。

## 2026-10-08：laptop 常驻 Worker 的原生 Shell 挑战

laptop 本节点的既有 authority/Worker 已实际完成独立固定 scope 的未知
文件哈希挑战。探针核对原任务账本、选中授权 inference profile、同一
native thread/turn、原生挑战命令及匹配 call_id 的哈希/文件名输出，
最终回复亦匹配；不是独立临时 Codex 或模型自报成功。复查相同 state
不重新提交。Chat/Live 主账号、Global Leader scope 与其它会话未改。

部署者同时观察到 owned Node systemd MainPID 及其 npm Node launcher
child，但没在 turn 结束前捕获其 native grandchild `/proc/exe`。因此
探针的 `systemd_unit_verified` / `runtime_process_executable_verified`
仍为 false；上述真实执行通过不能扩写为该项进程身份验收已通过。

官方 0.159.2 musl 完整包已在 laptop 下载成功并核对整个官方 SHA256；
完整保留布局的本地安装/文件复核已通过。云端 transfer、配置切换与
切换后常驻 Worker 的实际执行尚须各自核对，不能用此处本机结果代替。

加入 Node 可选运行包观测和本地分段 split/join 后，本机完整 501 项测试
通过。测试覆盖独立 Worker companion、缺件故障防重复、peer 命名隔离、
观测/可选账本读写失败不关闭健康 Worker、未知布局不阻断、布局和
heartbeat 不充当执行恢复，以及分段不完整/变化/整包 SHA256/发布竞态。
云端同版本全套测试和这一观测的实际部署仍待单独核对。

## 2026-10-08：云端完整运行包与常驻 Shell 修复已验收

- 官方 0.159.2 `x86_64-unknown-linux-musl` 完整 artifact 已传到云端。
  两个断开的分段保留原数据，先核对源/目标前缀 SHA256，再用原生 SFTP
  续传；八段拼合后核对整包大小 159961162 和官方完整 SHA256
  `9e2d29a713b94478b240dec2f10e11324cd05fad76dc43e7c639bdf8a1337a6b`。
  安装器已完整保留主程序、配套 helper、resources/PATH，并对归档与
  提取文件逐项复核通过；没有拼装不同来源或覆盖旧版。
- 云端 Worker 空闲时切换到该完整包，私有配置产生 0600 时间戳备份，
  旧源码和原运行包保留。只重启 owned Worker 和 companion，唯一云端
  iLink authority/receiver 的 MainPID 未变。实际 Worker unit 的
  `NoNewPrivileges=no` / `PrivateTmp=no` 已核对；既有 OS 权限仍适用。
- 切换后在该节点仅提交一次固定 ID 的独立只读未知哈希挑战，常驻
  Worker 实际 completed。原生会话、授权 profile、原任务 checkpoint、
  挑战命令、匹配 call_id 的未知文件哈希输出及最终回复全部核验通过；
  随后相同 state 只读复查仍通过，没有新建 ID 或使用 Global Leader
  scope。它与上节 laptop 的独立真实验收共同证明两端原生 Shell 可用。
- 本次验收期间，部署者额外观察到 owned Worker MainPID 的直接 native
  `codex` child，`/proc/exe` 指向完整包的实际入口。这与探针自身的
  `configured_runtime_identity_only` 是两种证据。完整 cgroup/unit 身份及
  `/proc/exe` 内容 hash 未在 child 退出前捕获，不能扩写成自动进程认证
  全部通过，探针的两个自动 process/systemd 核验字段仍是 false。
- 两端被动运行包观察已实际持久化：laptop 的间接布局为 unknown，未被
  阻止；云端 canonical 布局为 complete。被动观察的
  `runtime_execution_verified=false` 仅表示该观察不执行验收，不否定
  上面的独立 Shell 证据。运行包 complete 亦不证明 Native Live/语音
  或 native memories 后台提取已正常工作。
- 实际云端→laptop A2A link 的认证状态 connected/reachable、失败数 0，
  laptop→cloud 亦可达。云端 `mode=autonomous` 是远端 laptop 没有
  Global Leader 租约，而非传输断线；未选举或接管其它机器的 Leader。
- 同版功能源码的 501 项全套测试在本机 Python 3.14 与云端 Python
  3.6.8 都通过，GitHub CI 亦通过。云端首次一项 metadata 早期拒绝夹具
  遇到快速同长度写入的时间戳合并；改为显式可辨的 fixture mtime 后
  完整测试通过，生产规则未放宽，原测试的整包哈希拒绝发布始终有效。
- 修复通知经既有远端 ClawBot 通道、同一 request ID 查询达到 accepted。
  仅表示服务端受理，手机独立收件未确认；未重发、切号或新开接收器。

随后将 canonical 组件 incident 改为独立表、类型化事件及维护任务 ID，
并补入旧故障的保守迁移与 A2A/Leader 可达状态分离。最终节点功能源码
在本机和云端均完整通过 506 项测试；同名合法 peer 不清除组件故障，
布局恢复后升级不假报执行恢复，模糊旧证据/可选 schema 错误为 unknown，
不重放旧任务，也不阻止健康原生 Worker。A2A peer 可达但没有远端
Global Leader 的真实 HTTP fixture 单独验证了上述 mode 边界。

随后将旧 `local-worker` 启动/意外退出故障也迁至独立组件表，合法同名
peer 的 row、维护任务和重连事件保持独立。只有监督器拥有的同一代
Worker 稳定存活与启动后新鲜认证心跳才能清其启动故障；失败/恢复以
轻量锁和 failure generation 防并发误清，不把进程存活当 Shell 验收。
未来心跳、旧心跳、错误节点和已经失败的一代均不清故障。旧故障依据
固定事件链和原任务身份保守迁移，不创建/更改/重放旧任务或改旧 peer。
可选记录/状态/恢复失败不会关闭健康 native child；缺失或旧已恢复的
组件记录不能掩盖新的 core failure。新增十项回归并增强原稳定恢复测试
后，同功能源码在本机 Python 3.14 和云端 Python 3.6.8 均全套通过
516 项测试（Node 52 项）。这轮没有新提交模型任务、切换推理账户或
更改原生工具权限；实际服务加载及 CI 另在核对后记录。
