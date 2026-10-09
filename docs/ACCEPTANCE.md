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

上述功能提交 `b6edd8b` 已在两端实际加载：laptop owned Node 与 cloud
companion active，持久快照新鲜，分别报告 A2A 可达、运行包/Worker
incident 均 clear 且无 attention。cloud companion 的 worker_alive=false
表示它没有管理外部 Worker，并非外部进程死亡；独立 Worker 和唯一
iLink receiver 均 active，MainPID 与更新前相同。原生 reverse tunnel
亦未重启。旧源码完整保留，私有工作区、配置、账号和任务历史未移动。
原有 laptop/cloud 两份固定 state 的原生 Shell 挑战经默认只读复查仍
completed/verified，没有重新提交或新建任务；这是历史执行证据复核，
不是声称 laptop 新进程又运行了一次挑战。自动 process/systemd 认证
字段仍为 false，边界不变。功能提交的
[GitHub CI](https://github.com/gih10012/personal-assistant-mesh/actions/runs/37804221106)
已实际 success。此修复不代表自动 Shell 故障关闭、跨机 Global Leader
连续接管、资源最佳调度或完整 Native Live 已完成。

## 2026-10-09：隔离 Leader 跨机原生上下文接管

在独立的 loopback authority、临时私有账本与两端临时 Worker 中完成了
已结算任务边界的实际接管；没有切换正式业务 Leader、重启唯一 iLink
接收器、清空历史或替换原生会话。helper 本身不启动模型或控制服务，
owner 分别显式运行了两端 Worker，各自只执行一次 `run_once`。

- 云端 remember 任务最终 completed，仅回复 ACK；笔记本 recall 任务
  completed，并准确回复仅存在于前一轮原生上下文中的随机字串。
  新 recall 输入和持久账本 reference 均不含字串，Mesh memories/children
  为空。两个临时进程显式关闭 native memories，正式默认配置不变。
- 同一 canonical Codex thread 跨机器延续，两个 turn ID 不同；真实租约
  选举的 Leader 任期从 2 增到 3，没有人工更改期限或指定任期。
  云端成功任务 epoch 为 2，笔记本为 1；这不是同一活跃任务的重放。
- 两端选定 turn 的原生日志核对到实际输入、最终回复、成功结束和
  零工具调用。源完整日志 57,869 bytes，目的完整日志 104,231 bytes；
  private checkpoint、API 结果与 gzip 上传文件的原始内容哈希一致。
  笔记本 checkpoint/完整文件核对使用云端 isolated ledger 的 SQLite
  一致备份与仍运行的同一 authority API，未读取正式业务数据库内容。
- 首次云端尝试停在 account preflight，没有 thread/turn/effect marker。
  第一账户明确额度耗尽，第二账户快照失效。核对其与本机已授权备用
  账户的相同身份后，私有备份旧快照并仅同步该账户认证文件；随后通过
  同一不可变任务 ID 的正常 claim 续接，没有新 ID 绕过审查。
  此同步是 owner 的一次维护，**不是**已完成分布式 token 刷新代理。
- 验收器识别实际 Codex 自动环境 envelope，但只允许业务输入前的
  单条已知 metadata XML，cwd 精确匹配，不含 nonce/额外指令；未知
  记录仍 fail closed。这不改变 native Shell、文件或网络权限。

私有证据保留在本次两端独立临时目录内，未发布 nonce、任务输入、
rollout、数据库或认证文件。默认 observe 只读复查同一个固定 state；
观察超时不重建或重提任务。验证字段仍如实保留 process/systemd 自动
认证、独立物理宿主身份、生产 Leader scope、authority 宿主故障接管、
未决效果重放为 false。

这项证据证明 **正常云端 authority 尚在时，已结算的 Leader 原生会话
可随选定完整上下文从云端到笔记本接管**。不证明业务中未知外部效果
可自动恢复、不证明权威主机 HA、不证明 Pi/离线模型或全网最佳调度。

同轮新增双代 Leader fencing、共享容量预留/provider journal API、
agent 与 CLI 入口和固定分类候选诊断；617 项全套本机测试通过。
容量/回执测试包括真实 loopback HTTP 认证，但不是实际工具执行；
provider adapter 和独立结果验证继续实现，不把这些回执计为完成。

### 同轮正式版本部署与启动竞态

`06cf088` 的同一冻结源码在本机 Python 3.14 和云端 Python 3.6.8
分别通过 617 项完整测试，公开
[GitHub CI](https://github.com/gih10012/personal-assistant-mesh/actions/runs/37811446369)
实际 success。之后才在无 running 任务、无 submitting 消息的边界更新
正式云端 authority/worker/companion 与笔记本 node/worker。正式升级
与上面的隔离接管是两个阶段：升级时唯一接收器确实有一次受控重启，
不能把隔离验收阶段的“不重启接收器”延伸为全轮声明。

部署前一致 SQLite 备份与云端旧源码完整保留；升级后云端原有
12 completed / 5 failed 任务、笔记本 4 completed 任务没有改写。
原有 27 accepted 消息记录保留；随后发出的唯一阶段通知有稳定防重
ID，按同 ID 查询确认服务 accepted，累计为 28 accepted，但手机收件
未独立验证。私有 Unix reverse tunnel 保持原进程，不新增公开入口，
也没有使用阿里云控制台或 EaseCation RAM 账号。

正式服务同时启动时，cloud companion 首次遇到 SQLite
`database schema has changed`，systemd 在既有退避规则下重启一次
后恢复。这是实际暴露的初始化竞态，不能把 active 状态当作没有故障。
根因是 `executescript` 隐式提交已有 writer transaction，使 DDL 与
check-then-ALTER 迁移不再原子。后续修复改为同一事务内逐条静态 SQL，
并保证 setup 失败关闭连接；只对 WAL 设置已知锁错误用单一十秒预算
重试，不重放任何业务 SQL。新增 14 项测试包括真实 8-thread / 6-spawn-
process 冷启动与旧表迁移、失败回滚、历史保留和 setup 关闭连接。

同时新增可选的 owner-installed provider callback 库与 28 项测试，
包含真实本地 SHA 读取、loopback HTTP、子进程退出后不重放和结算
响应丢失后的同 ID 恢复。这些本地测试不证明两台宿主的实际受管能力
执行或独立性能验证，也不把库误称为常驻全网调度器。原生 Shell、
MCP、联网与文件功能不受新增 callback journal 限制。

修复后的本机 Python 3.14.7 完整回归为 659 项通过；云端兼容性和
这一后续版本的正式部署须分别核验，不从本机测试结果推断。

### `92016d7` 的后续部署、重启和 CI 核验

同一冻结归档在云端 Python 3.6.8 也实际通过 659 项。空闲边界升级
云端三项 user service 后，authority、worker 和 companion 都 active，
各自 `NRestarts=0`；Store/Network/provider 文件哈希与本机发布版一致。
保留一致 SQLite 备份和完整旧源码；只读比较备份中的原任务 ID、
input、epoch、status、result，未发现缺失或改写。此时云端累计
15 completed / 5 failed、33 accepted；新增任务/消息不是历史丢失。
真实认证 CLI 可读取空容量目录，不把它误报为已有能力执行。

笔记本随后已由系统重启，node/worker/private reverse tunnel 三项
user service 自动恢复，均无额外重启；启动在本次源码落盘之后，
core 文件哈希一致，启动日志无 SQLite 初始化错误。cloud 经原私有
Unix reverse socket 的认证 `mesh-hello` 实际返回 laptop / laptop
与自有 `mesh-a2a/1`，证明这一连接恢复，不证明新增标准 A2A 支持。
没有为此再次运行模型挑战或主动重启隧道。

系统重启清掉本机 `/tmp` 的派生验收报告，但选定原生会话日志仍为
104,231 bytes，哈希与接管时完全一致；原生连续记忆没有随临时目录
一起丢失。云端同一 state、闭合账本/一致快照、原 report 和验收源码
仍在，已复制到两端私有持久档案；本机选定原生日志也另存私有副本。
不重提原任务，不把丢失的派生 report 伪称为仍保留；后续新 probe
使用私有 `/var/tmp` 工作目录并另作持久备份，认证值不在这些档案中。

公开 [CI run](https://github.com/gih10012/personal-assistant-mesh/actions/runs/37814749586)
并非全绿：Python 3.12 的既有 artifact deadline 测试实际失败。
socket 超时抢先于整体截止检查，误报为 transport failure；两台宿主
通过不能替代这个检查。后续修复只在真实 timeout 且整体 deadline
已过时改报 deadline，其他网络/HTTP 错误保留原分类，并补确定性测试；
修复的整套回归和新 CI 还须单独验收。

`7e4a022` 后续冻结归档在本机 Python 3.14.7 与云端 Python 3.6.8
分别通过 664 项完整测试；修复后的
[CI](https://github.com/gih10012/personal-assistant-mesh/actions/runs/37862375692)
在 Python 3.8、3.12、3.14 三个 job 均实际 success。新增 5 项确定性
回归覆盖 open/read 的真实 timeout、URLError 包装、截止前超时、
连接重置/普通 OSError 和 HTTP503；checkpoint 与既有输出保护未放宽。

### `c9a084e` 与跨宿主受管能力真实执行

同一冻结源码在笔记本 Python 3.14.7 与 VPS Python 3.6.8 分别通过
701 项完整测试；公开
[CI](https://github.com/gih10012/personal-assistant-mesh/actions/runs/37863301228)
的 Python 3.8、3.12、3.14 三项也实际 success。新增并发 provider WAL
开库回归及显式隔离验收器，不重试 BEGIN、业务写入或 callback。

随后使用这份源码，在现有两台自有宿主实际走通：独立 authority 的
认证能力登记/观测与 exact grant → 唯一父任务的真实 claim/epoch →
共享逻辑 slot 预留 → VPS callback 实际读取 64-byte 随机文件并计算
SHA → 本地 journal 先落盘 → 原 receipt 结算 → 容量释放。这里不是
内存 API 或 loopback fixtures；笔记本通过独立私有 SSH forward 调用
VPS 上无 iLink、无模型 Worker 的测试 authority。

输入的 owner reference 只在笔记本保留，没有复制到执行宿主。
笔记本用闭合 VPS provider journal 和实际 API 核对实际结果：独立
保留的参考一致、authority 状态 completed、本地 settled、容量已
释放。再次运行同一 execute 仅查询终态：账本前后 SHA 完全一致，
只有一个 execution row，没有 callback 重放或新 operation ID。
task/Leader admission epochs 均来自真实 claim，不填造更高任期。

这只独立核对文件输入/结果，**不证明** CPU/GPU 性能、真实机级
强制配额、全网最佳调度或模型自主创建/安装/选择该工具。core 的
execution_verification 仍为 provider_reported，independent performance、
physical-host attestation 与独立 quiescence 字段仍为 false。
单 slot pool 是 owner 的逻辑 admission 合同，不是 CPU 使用率限制。

验收在能力结算后结束；父任务租约随后到期，owner 只暂停了这一个
隔离 fixture 父任务，没有假报父任务 completed，也没有暂停当前
personal-assistant-mesh goal。闭合账本、一致 authority snapshot、
报告和冻结源码已另存两端 0600 的私有持久档案；档案不含 Mesh token
或 native auth，owner reference 仍只在 owner 端档案中。只终止该
probe 的独立进程和 forward，正式 authority/worker/node 与既有
reverse tunnel 不受影响。测试入口 17683 已关闭；正式云端三项
service active 且 NRestarts=0。没有新公开端口、付费资源或 API key。

### `d2770b8` 可选独立运行器与正式源码升级

可选 owner manifest 运行器、独立 CLI、service example 和 43 项新增
测试已冻结在 `d2770b8`。同一归档在本机 Python 3.14.7 与 VPS Python
3.6.8 分别实际通过 744 项完整测试；公开
[CI](https://github.com/gih10012/personal-assistant-mesh/actions/runs/37865484701)
的 Python 3.8、3.12、3.14 也均为 success。Python 3.6 的验证是源码
运行兼容性证据，不修改 package 声明的 Python >= 3.8 安装要求。

回归包括私有完整绑定/UTF-8 源码快照、同源码更换入口或位置不得
冒充旧 epoch、顶层代码仅在 admission 后执行、未知不重放、旧结果
在升级或移除模块后同 ID 补结算，以及 stop 期间只完成当前 callback
而不启动下一项。交叉审查实测并修复了旧未接收 epoch 的请求阻断新
请求及晚结算的问题；只隔离特定合同版本不匹配，畸形响应仍报错。

VPS 对正式 authority 的额外 smoke 使用新私有 journal、空 adapters
和现有本人 credential-bound Client，实际运行 describe 与两轮 finite
serve，exit 0、diagnostics 空、execution rows 为 0。这核对 CLI 启动、
认证轮询及持久 namespace，不冒称已执行工具、启用常驻 service 或
模型自主部署。没有打开 native auth、请求 API key 或启动模型。

确认无活跃任务/待提交消息后才更新正式 VPS 源码，保留一致 SQLite
备份和完整旧源码；authority、worker、companion 三项都 active，
NRestarts=0，healthz 实际 ok。对备份所有原 task 的 ID/input/epoch/
status/result 只读比较，缺失或改写数为 0；此时累计 17 completed /
5 failed、37 accepted，后续正常新任务不能误算作旧记录被改写。
provider、runtime、CLI 文件 SHA 与冻结版一致。正式升级包括一次
受控的唯一接收器重启，不能把隔离 probe 的“不重启”延伸到本阶段。
新 provider service 仍只是可选 example，不默认启用。

本机全套曾有一条 unclosed SQLite ResourceWarning。带 allocation
traceback 的同版本 744 项复查通过，定位是旧 schema migration 的
测试 fixture：sqlite3 connection 的 with 只管理事务，并不关闭连接，
不是生产 Store/provider/runtime 分配泄漏。后续 fixture 用 closing
并保留内层事务 commit/rollback 语义；不以测试通过宣称所有生产
依赖或资源生命周期已获得独立审计。

笔记本 node/worker 随后也在无执行中任务、无 submitting 出站记录的
边界重启，三项服务（含原 reverse tunnel）active 且 NRestarts=0；
reverse tunnel 仍是原进程。对一致备份的原 task 与 outbox 逐项比较，
缺失或改写数均为 0。直接 loopback healthz 为 ok；cloud 经原私有
Unix socket 的认证 hello 实际返回 laptop/laptop、mesh-a2a/1。
一次普通 curl 受调用端环境代理影响返回 502，定向 bypass loopback
后核对成功；Mesh Client 本已显式禁用该类环境代理，未改全局网络
设置，也没有因此再次重启服务。

另发现实际 **8 条本地 pending 通知（7 result、1 review）未尝试发送**，
不是手机收件，也不是已被消费的审计记录。无第二 iLink 接收器的
本地 authority 不启动 outbox send consumer；Node 的 A2A task report
与此队列分离，已有 accepted A2A report 不等于微信通知送达。这是
通知 relay 缺口，不把静止 pending 误判为 in-flight，也不清空它们。
后续须显式区分私有工作/audit 与本人通知、按凭据权限选择投递路径，
保留 source-node/原 outbox ID/内容指纹并查询云端状态；历史 8 条不能
自动批量重投或另开接收器。原生通信工具仍不受这个 Mesh 缺口限制。

### `51c2f21` 节点向绑定本人的通知回传

2026-10-09，新增可选的 `mesh-notify/1` 文字入口与独立节点 relay
线程。显式通知与自动任务结果分开；private 节点的自动结果仅本机
记录，历史 outbox 不自动 enrollment。云端已有 laptop `agent_peer`
以及本机 worker 明确增加 `owner.notify`；没有授予 operator 身份或
选择收件人的能力，没有启动第二个 iLink receiver。

冻结树为 `0c54749971cb4dd1f76cec557e21aa39c93b0a11`，随后发布为
`51c2f21cf0ce28642510612a23873528e0d32eb6`。同一源码归档的 SHA-256：
`338d624bd54e08ed621f682c3feee3667967b0b351c03aa240cbc86ee1c6c8da`。
本机 Python 3.14.7 实际通过 **843 项**完整测试（101.643 秒），VPS
Python 3.6.8 也实际通过 **843 项**（107.590 秒）。公开
[CI](https://github.com/gih10012/personal-assistant-mesh/actions/runs/37871322088)
的 Python 3.8、3.12、3.14 全部 success；不因此改变 package 原有
Python >= 3.8 的安装要求。

新增测试包括 authenticated loopback HTTP、同 ID 并发原子去重、
真实子进程在云端入账后退出及冷恢复、私有跨进程 flock、损坏回执、
权限撤销、旧记录保守迁移、挂住的通知不阻断 Node.step、令牌轮换
不改变固定目标，以及停止发生在提交意图落盘后的保守隔离。

交叉复核找到并实测修复了两类边界：查询后换账号的 TOCTOU 必须
在实际 POST 入队前再次检查 route_id；同账号换空账本也必须改变
持久接收实例标识。仅靠实例标识仍不能检测同实例旧快照回滚，因此
本机在 POST 前同事务持久化 `post_attempted=1`，后续回执消失时
只查询、不自动重新入队。真实 HTTP/SQLite 旧快照恢复回归、提交
在到达云端前断开的回归均验证这种保守行为；尚未尝试 POST 的通知
仍可在查询网络恢复后首次提交。

这不是一般性微信快照反回滚保证：若直接恢复实际发送前的旧
`channel/pending` 行并盲启发送器，或者本机 intent 也一起回滚/丢失，
仍需独立核对和隔离。没有自动清库、恢复生产快照或解除 unknown。
完整规则见 [通知合同](OWNER-NOTIFICATIONS.md)。

两端先 dry-run，再在 running task/submitting outbox 均为 0 的空闲
边界做受控升级。私有一致 SQLite 备份为：

- VPS：`~/.local/state/personal-assistant-mesh/backup-51c2f21-20261009T014700Z/ledger.sqlite`。
- 本机：`~/.local/state/personal-assistant-node/backup-51c2f21-20261009T014800Z/ledger.sqlite`。

备份目录 0700、数据库 0600；云端旧完整源码保留为
`~/.local/share/personal-assistant-mesh.before-51c2f21`，配置更新各自
保留旧私有配置。两端按备份所有原 task/outbox 列逐项比较，缺失或
改写数都为 **0**。当时云端 23 completed/5 failed、49 accepted；
本机 5 completed、原有 8 pending/channel 均保留，不自动转成 relay。

VPS authority/worker/companion 和本机 node/worker 都 active，
NRestarts=0；本机原 reverse tunnel PID 1642 未重启。两端 loopback
healthz 都 ok；cloud 经原私有 Unix socket 的认证 hello 实际返回
laptop/laptop、mesh-a2a/1。本机 peer 对云端通知 status 的认证查询
实际返回 mesh-notify/1、cloud/laptop、not_found、idempotent_receive
true；只读 readiness 查询没有创建远端通知或回执。

随后只发了一条已授权的本人进展通知，稳定请求 ID 为
`mesh-owner-relay-51c2f21-20261009`。路径是本机 owner notify 入队 →
独立 relay → 云端 credential-bound 原子接收 → 原有唯一微信通道。
本机持久 relay 最终 accepted，attempt=2（首次提交/后续查询，
不是两次微信发送）、post_attempted=1、error=null；云端该来源/ID
只有 **1 条**回执，通道 accepted、context retry_count=0，累计
accepted 变为 50。历史 8 条本机 pending 仍保留，没有批量补发。

本次实际验证到微信协议服务端接受为止，所有回执仍
`delivery_verified=false`；没有手机收件或单次显示证明，没有追加
桌面历史/通话/媒体验收。未请求新 API key、付费资源或模型推理，
未改全局代理、桌面/PIN、原生工具权限或连续原生会话。

本机 relay 使用独立持久状态，底层 outbox 行不借用微信 sender 的
状态更新；应通过 notify/status 查看其 accepted 回执，不能把原始
outbox 的 pending 合计当作未送达证明。idle 也不表示云端通道健康。

### 2026-10-09：原生模型创建工具、功能升级与新 epoch 回退

模型由既有 laptop Worker 在原生 Codex 项目 thread 中实际编写并测试
一个有界、只读的文件/内容分块统计工具 v1；后续任务继续**同一 thread**，
生成 standalone v2，增加逐文件重复/跨文件复用与贡献统计。v1/v2 是
真实功能变化，不是 owner 手写 callback 后仅让模型修改版本标签。
实际 author tests v1 18 项、v2 27 项在 laptop 与 VPS Python 3.6.8
均通过。源码 SHA-256 分别为：

- v1：`fdb83593c29fea75dfa7bcb992e1cc2f137827a03d935f9bca89ea3177ab82d5`。
- v2：`440eab2def6ec6749209b25cda5b6e220b28843cf307db49316b723e40c0faf6`。

新生命周期/observer 功能的冻结代码在本机 Python 3.14.7 与 VPS
Python 3.6.8 各通过 **905 项**完整测试。该次归档没有随后新增的网络
inventory 和持续工作指令；不可把 905 外推成它们的测试证据。

Owner 在模型源码冻结后生成新的合成随机文件和独立参考答案；参考
答案/计算代码只保留在 laptop，未复制到执行宿主。隔离 VPS authority
上实际完成 capability epoch **1 → 2 → 3**：v1 发布/激活/执行，v2
发布/激活/执行，旧 v1 **保留相同字节但使用新 epoch、新安装位置**回退
并再次执行。三个原 operation ID 分别结算 completed，真实结果文件
与 owner 独立参考一致，持久 provider journal settled、容量释放。
v1 终态再次轮询没有第二次 dispatch，账本/输出 SHA 不变。

最终只读 observer 从三个闭合 journal、实际认证 API 与结果文件核对
全部声明的结果和结算。v1/v2 当前目录 binding 已被 epoch 3 取代，
不能把历史 activation 冒充当前安装或独立历史目录证明。执行器和
authority 的 admission task/Leader epochs 来自真实 claim，不填造任期。

这证明**实际模型创作 + owner 编排版本生命周期 + 跨宿主执行与独立
结果核对**，不证明模型已自主决定授权/预留/部署整个链路。测试由
owner 显式配置 pool/grant/观测和安装 manifest；单 slot 是逻辑准入，
不是 CPU 强制配额。核心 verification 仍为 provider_reported，独立
性能、物理宿主认证、quiescence、native 模型来源 attestation 都未证明。
单模块版本管理也不是所有插件依赖/安装委托的完整生命周期。

本次使用独立私有 SSH forward 和无微信/模型 Worker 的测试 authority。
正式 VPS/laptop 服务及原 reverse tunnel 未重启；无新公开端口、API
key 或付费资源，未改变 native Shell/MCP/网络权限。原生 rollout、
原 task/thread/turn 标识、配置、令牌及 owner 参考留在私有记录中，不
进入本公开仓库；本次不复制 auth 到 VPS，也不发送新的微信通知。

### 2026-10-09：网络元数据盘点与连续工作合同（新增集成）

只读 collector 在实际 laptop 与 VPS 均得到 Linux 元数据：笔记本的
已连接 Wi-Fi、VPS 的已连接虚拟以太接口各承载主表 IPv4 默认路由；
未发现主表 IPv6 默认路由。两端诊断类别为空。笔记本有六个代理变量
名称，VPS 无相应变量；不读/返回代理值，不证明程序已使用代理。
实际采样已完成，不自动登记 candidate、改变联网或主动测试互联网。

网络模块 21 项新增测试，与 networking/resources 合计 70 项通过；
CLI 接入后相关网络/tool/provider/resource CLI 合计 54 项通过。新增
持续工作 developerInstructions 在原生 start/resume 请求中保留 custom
原文和原 thread，9 项新增请求 fixture 与既有协议/权限/memory 共
22 项通过。这是请求组成证据，**不是生产 Worker 已加载、模型自主
选型已运行或 3–5 天 ChatGPT schedule 已创建**。

#### `b1a10fa` 公开发布

随后集成了网络 inventory/CLI 和持续工作指令。冻结树为
`a5837499404beb2b9421fc69fd90e75a6233c338`，归档 SHA-256 为
`20ea16ba310bdf41e99f585a3f839d0c216ecc4256b0d2a59345e333e79d7c04`。
同一归档在 laptop Python 3.14.7 完整通过 **941 项**（118.682 秒），
VPS Python 3.6.8 完整通过 **941 项**（108.984 秒）。已公开发布为
`b1a10fa453e334c175a8203d35326770ba9757a8`，
[CI](https://github.com/gih10012/personal-assistant-mesh/actions/runs/37891035551)
Python 3.8/3.12/3.14 均 success。该次代码尚不含后续 federation 模块。

工具验收结束时，owner 按原 operation ID 再次认证核对三个 completed
效果、容量未持有，只暂停最后一个隔离合成父任务的过期 bookkeeping，
不假报其 completed。仅精确停止验收 authority 和临时 forward；正式
VPS 三项 user service PID 均仍为原 PID，NRestarts=0，验收端口已关闭。
结果、闭合 journal、冻结源码与独立 owner 参考另外保留在私有持久
目录（0700/0600）；owner 参考仍只在 laptop，未复制 auth 或公开日志。

### `7e569cf`：来源能力同步、真实断连与冷恢复重汇合

可选 source export / issuer-isolated projection / 独立 Node 同步循环
和模型/CLI 只读入口已公开发布为
`7e569cf7af0152b2d9681fbf41b4263a7858c16c`。冻结树为
`a4cfe8da32c485e5040cd474cf9d60c8c0381c96`，同一归档 SHA-256
`928f543e06b5eec826c09f175286110ac984c5121bcced62c7f04a0c9928804c`。
laptop Python 3.14.7 完整通过 **1002 项**（129.047 秒），VPS Python
3.6.8 完整通过 **1002 项**（127.468 秒）。公开
[CI](https://github.com/gih10012/personal-assistant-mesh/actions/runs/37892421063)
另按其真实状态核对，不以提交或排队声称 success。

并行独立审查实际复现了规范页 JSON 与 HTTP 带空格 wire 大小不一致：
一个合规接近 8MiB 的导出页会被客户端拒绝，游标无法推进。修正为
compact HTTP JSON，保留原上限；真实 HTTP 的 300 个大公告分页回归
验证首部分页可读、续页总数正确。source/projection/resources 共 67
项通过；独立新接线 16 项、相关回归 77 项通过，Root 的 Node/CLI
组合回归 85 项通过。测试和实际部署证据不混同。

真实跨宿主验收仍使用隔离、无模型 Worker/微信的 VPS authority 和私有
SSH forward，不操作正式节点配置。源 VPS 的实际只读网络采样生成一项
`network.egress` 元数据候选，owner 明确登记到测试 Registry；未安装
出口执行器、未验证互联网或性能、未自动注册正式资源。laptop 保存
同名的本地 Wi-Fi 能力、一个 pending 本地任务和一条**合成 unknown
意图**（未执行 SSH 效果），随后用已授权目录 credential 实际拉取源页。

精确终止该临时 forward 后，实际同步失败、projection 连接标记
disconnected、查询隐藏远端候选；原 issuer/cursor/退避保留。VPS 源端
在断连时撤销原能力，**执行 epoch 仍是 1，mutation revision 从 1
增至 2**。重新建立同一路径，laptop 新进程从同一私有 ledger 冷恢复，
读取原游标和退避；下一页得到 tombstone。旧原始页再次 apply 被拒绝，
撤销未复活。本地 Registry、原 task 和合成 unknown 行逐字段均未改写。
没有为“恢复成功”清 cursor、改 issuer、复制 grant/pool 或换 operation ID。

这是**实际 SSH 路径跨主机目录同步与冷恢复重汇合**，不是所有网络
分区/真实 Wi-Fi 自动重连、已安装正式 Node 循环、模型离线推理、真实
外部 unknown 效果的执行、全局共识或自主最佳调度的验收。投影保留
declared_estimate，未补偿未知传输延迟，亦不证明真实资源租约仍有效；
资源 owner 仍负责每次真实准入、授权和结算。

验收结束只停止对应测试 authority/forward，正式 VPS 三项 user service
及 laptop node/worker/原 reverse tunnel 均保留原 PID、active/NRestarts=0。
测试 loopback 端口关闭。源/目标 ledger、一致快照、报告与冻结代码另存
两端私有持久档案（0700/0600），没有将测试令牌、原生 auth、个人网络
地址或聊天发布到 GitHub。未请求新 API key、支付资源或微信发送。
