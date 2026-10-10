# 持续执行计划

更新时间：2026-10-10。长期 goal 仍 active；以下是持续推进的优先级，
不是为了宣布完成而缩小目标。有效 owner 目标见 [GOAL](GOAL.md)，
任务状态与续接点见 [TASKS.json](TASKS.json)，实测交接见
[2026-10-10 报告](STATUS-2026-10-10.md)。

## 最新 owner 方向

先做可组合的底层和中层，再让 Mesh 自己承接 Live 等应用项目。
产品对标 Dots；原生 Codex 优先、Pi 可选，不以 OpenClaw/OpenCode 作为
Leader。面板只是观察入口。原生 Shell、网络、文件和 MCP 不由 Mesh
拦截。资源、出口、性能、推理和工具都能力化，由模型判断与组合。

目标是聚合时像一台机器/一个聪明人，分区后各节点自主工作/联网/
重汇合；薄的统一能力入口，而非给原生功能加白名单。VPS 为受管
ChatGPT/模型流量的默认推荐公网出口，校园节点为校内目的地出口；
不阻断 native 备用网络。ClawBot 是可迁移承载的沟通能力，不绑定
VPS/Leader 工作区。内建 node ID `cloud` 保留且指 Alibaba VPS，展示
`ali-vps`；OpenAI 官方 Codex Cloud 单列 `openai-codex-cloud`。

不要为所有部署环境预先写完固定适配器。Windows、手机、临时云容器
按实际环境发现能力，由该节点 agent 基于仓库和接入合同自行改装。
测试保护协议、身份、未知副作用与状态恢复，不把 Linux 示例变成
所有环境都必须照抄的运行方式。

## 执行顺序与并行分工

| 阶段 | Task | 当前工作与依赖 | 验收依据 |
| --- | --- | --- | --- |
| 已发布底座 | PAM-001 | b1a10fa 已发布，941 项两端完整测试与 CI 通过；实际 v1/v2/新 epoch 回退已核验 | 两端测试、源码版本、真实结果/结算/容量释放，不用自报替代 |
| 中层能力基础 | PAM-002 | 盘点已完成；优先真实 VPS 公网路径与本机校园内网路径，不能停在 declared 候选 | 网页/API/推理与校内目标分别实际验收；目录/TTL 不代替联网 |
| 去中心化恢复 | PAM-003 | 003a/b/c 已发布；003d 正式两端已备份加载，双向独立循环连续 `ok`；继续真实目录/故障域观察 | 单节点继续工作；旧消息/撤销/未知效果不因恢复重放；真实断连/重入网 |
| 自主资源调度 | PAM-004 | 004b/c 固定 GET 已实际选择/自动执行/核验；004d 推进统一入口与真正多候选出口 | 不外推通用联网；一次授权后日常易用，未知副作用不重放 |
| 异构节点适配 | PAM-005 | 定义 enrollment/运行期/能力/持久状态/适配报告合同，AI 按 OS 改装 | 先一个不同环境的真实接入；不要求先写完全部 Windows/手机功能 |
| 多入口与临时节点 | PAM-006 | Firefox 手动、本机 Codex MCP 往返已实测；006d Chat 原生工具、006e 官方 Cloud 实际 job 分别继续 | 不是以 VPS 在线替代 Cloud；临时 job 结束持久状态仍在 Mesh |
| 持续先进性 | PAM-007 | 原 Leader 同 thread 的真实 native plan 已验收；继续 goal 多 turn 生命周期，再配置 3–5 天检查 | 有实际计划/task/检查记录；新模型/版本的能力与访问实际验证 |
| 上层项目委派 | PAM-008 | Live、通话、面板及其他应用作为后续 Mesh 项目 | 由 Mesh 自己建 task、分工和集成，不由当前 Root 先包办所有应用 |
| 沟通入口容灾 | PAM-009 | 当前 VPS 单收发；优先候选承载/私有 checkpoint/受控交接，再故障接管 | 旧 holder 失权、原消息/task/unknown 连续；不启两个重复 poller |
| 多模型能力 | PAM-010 | Codex 显式 Responses、Pi 私有配置/目录/选择与成本合同源码候选已实现；未部署 | Responses 与 Pi 多协议的真实工具往返、续接、成本/隐私边界 |

### 本次重新排定的下一片

这些是协调任务，不冒充已提交给 native Leader 的执行回执。
独立专员并行研究/实现，Root 集成；PAM-007 不能串行阻塞其他底层：

1. **009a/b/c** 沟通承载：本机/VPS 候选和 checkpoint 合同；先隔离
   失权/交接测试，再单条合成消息的安全 handoff。当前不启动第二
   接收器、不改投旧 unknown。参见 [HA](COMMUNICATION-CARRIER-HA.md)。
2. **002a/b、004d** 网络：安装受管 VPS 通用路径、校园选择性出口，
   不全局改代理。Chat 网页/认证/API/真实推理、校内目标分别验证。
   本次直连 HEAD：ChatGPT 403、OpenAI models 401，仅证 TLS 到达。
3. **010a/b/c** 模型：Codex 放开显式 Responses provider；Pi 接隔离
   多协议配置和目录。先无秘密工具任务，不开账单/自动充值，不上传
   完整私人记忆给训练用途服务。见 [研究](MODEL-PROVIDERS-RESEARCH.md)。
4. **006d/e** Chat 原生 tools/OAuth 与官方 Codex Cloud 单次 job 分开
   核对账号/环境/连接/结果；缺外部授权及时问，不绕成 VPS Cloud。
5. **007c/f/e** 完整原生 goal 交接、剩余入口版本隔离与 3–5 天真实
   周期检查另并行推进；适度验收后推进，不陷入无限夹具完善。

现在底层互联可用、局部工作可继续，但统一联网、ClawBot 接管、任意
节点同一智能体体验尚未完成。不会用文档重排冒充新部署。

PAM-002 两端采样已完成；正式原 Leader 又接到有稳定 ID 的网络盘点交接，
已在同一原生 thread 用 A2A 建立 laptop specialist，真实等待、续接并
汇总两端完成结果。候选仍是 declared 历史采样，不是已安装出口。
PAM-003a/b/c 已发布，003c 隔离 SSH 断连/冷恢复/重联已验证；003d
正式两端已加载 06474df（功能代码与 7e569cf 相同），双向循环连续 `ok`。
重启前后原任务、原生会话、消息与执行账本指纹一致；没有新 receiver、
执行 grant、付费资源或隧道。此前正式目录为空；本次已登记一个 cloud
受管能力并在 laptop 认证 projection 实际读到它，但这仍不是 remote
执行授权、业务执行或全局 HA。PAM-004a 的 task-bound 证据/不可变决定/原执行身份
关联和薄模型/CLI 入口已实现，保留原 native thread。冻结代码两端
1053 项完整通过并正式加载；PAM-004b 的单目标受管出口现已通过下述
原模型选择/执行/独立产物验收，不外推通用联网。PAM-006
owner-bound ingress 提交/状态及受限成果 HTTP/CLI 合同已实现，入口/成果/
新诊断合计 95 项测试通过。cf308b3 冻结代码两端各 1148 项通过，公开 CI
37949397598 三个 Python 版本成功，laptop/VPS 已正式加载；重启边界原任务、
原生会话与执行表指纹保留，原隧道和 cloud node companion 未重启。
owner-terminal 实际提交/原 ID 重试、原 Leader 原线程 completed epoch 1、
Root 审查摘要发布与原入口读回全链路已通过。该入口不是 ChatGPT 账号。
Firefox 155 的本人主账号已在账户设置核对，开发 XPI 已临时加载，专用
ingress peer/private host 已装；按授权进行人工确认后浏览器只提交一次，实际 accepted
回执 `task_created=true`，按原稳定 ID 的状态查询也已验证。原任务截至
2026-10-10 曾观察为 `waiting_backend` epoch 5，失败在原生 history restore，
不是浏览器收账失败。Root 已在 epoch 15 静止边界核验严格旧前缀、终结
原生 turn 和未变源引用，保留完整旧字节后原路径原子刷新；前后任务/原生
authority 行一致，效果标记未改、DB 未恢复。原请求随后实际 completed
epoch 16，保留原 Leader thread；Root 独立审查、发布受限摘要，Firefox
按原 request 查到 completed 并读回批准成果，publication/task/request
精确匹配，手动 browser 全往返已验证。这不是 Chat 模型原生 tools。
成果 execution/artifact-content/account verified 均保留 false。Cloud 原生只读
login/list 通过，官方本项目环境查询 HTTP 200 空数组；该查询未返回
本项目环境，未提交 job。Codex 薄 MCP 已实现且 32 项离线测试通过，
已在本机一个既有 profile 安装，实际原生 MCP submit/status/approved-result
闭环通过；验证客户端没有模型 turn，不声称模型已自主选择调用 MCP。
官方 Chat 模型 tools、OAuth/Cloud 连接仍未验收。
Root 负责发布、计划与集成。研究、实现、
已安装、实际运行、独立核验分别记录。失败只改变相关 task 的下一步，
不默默丢掉原目标、不新建 ID 绕过 unknown。

## Leader 持续工作合同

### 2026-10-10 后续实际实施切片

- **002a** 已有独立 `egress probe/run/hold`：owned loopback SOCKS5h、
  远端 DNS、仅 child 环境；最终源码实机 laptop→VPS→GitHub GET 200、
  TLS 0，12,327 字节与独立 Git oracle SHA 相同。17 项聚焦测试本机/
  VPS Python3.6.8 通过。Chat403/API401仅到达；能力登记/统一便捷入口、
  第二执行环境、现有认证推理和 campus002b 尚缺。[出口](EGRESS-PATH.md)
- **010b/c 有限源码**：Codex custom Responses 与显式私有 profile、
  原 provider identity 续接保护；Pi 原生目录/同 route 选模、私有
  agent_dir/models.json、原 session；共用精确、过期可撤销的成本
  声明。默认订阅/native权限不变，不创建 key/费用。本机 native
  0.162.1 实际配置初始化/读回/空 loaded list/自然0已通过，零模型；
  Pi/第三方 stream→tool→原会话尚未实测。010a统一目录和010d实际
  推理仍推进。[Codex](CODEX-PROVIDERS.md)、[Pi](PI-PROVIDERS.md)
- **009b 有限源码**：单 BEGIN 只读私有 checkpoint，原任务树/
  authority/游标/context/出站ID/unknown保留；其他执行关系仅SHA锚点。
  格式强制不可迁移，不是新receiver或接管；旧承载失权/完整历史/
  实际handoff由009c/d继续。[checkpoint](CHANNEL-CHECKPOINT.md)

本片下一步冻结双端完整测试/公开CI，再将通用出口接到统一能力入口
和实际承载；009c排他/受控交接另并行。正式核心仍3198c87，不把
开发文件/配置初始化/单路径GET外推为已安装的全网智能或HA。

1. 开始/续接先读当前 goal、plan、任务账本和各节点真实状态，保留同一
   Leader 原生 thread；按项目复用子 agent，压缩使用原生机制。
2. 拆出有 ID、负责人、依赖、状态、下一步与验收证据的 task。推进一段
   就更新持久状态，避免仅在聊天里说“下一步”。不要让已完成阶段的
   文档/测试循环挤占新能力、去中心化恢复和自主调度的实现。
3. 模型判断用 Luna 或其他可访问模型；实时目录是候选，不是权限证明。
   综合当前任务质量、延迟、额度、环境和已验证能力，不把模型名/排序
   固化成永久决策。未知或已起效的 turn 不因切模型/换节点重放。
4. 每 3–5 天检查 OpenAI 官方发布、Codex 版本、协议、模型与 skill/
   插件的新思路；官方缺项时读 GitHub 主仓源码/许可证与必要论文。
   记录来源、版本、时间、适用性和下一步，不只收集热门名词。
5. 在既有 owner 授权和零新增支出内，自主升级、试用和改进；先在隔离
   安装/项目环境验证，保留回退与连续会话。破坏性升级、全局网络修改、
   新费用、密钥创建或第三方授权扩展仍须另外确认。
6. 发现环境差异由节点 agent 个性化改装；公开协议与能力入口保持稳定。
   Agent 原生排障/联网能力持续可用，受管调用只是推荐和受控路径。
7. 网络分区时局部自主，不假装全局一致；资源所有权、写入域、授予期限
   和未决结果都保留。恢复后逐条对账，不能合并 SQLite 就宣称共识。

## 调度与前沿定时任务的外部入口

优先将定时检查接到 owner 主账号的同一 ChatGPT 会话，并通过已连接
插件访问 Mesh。官方文档确认 web scheduled tasks 可以使用该会话的
已连接工具/skills/plugins，并能回到原聊天；web 不持续保存本机文件夹。
因此仓库和持久状态必须由 Mesh/已连接服务提供，不能把 Chat 当常驻
机器或“无限额度”的保证。[Scheduled tasks](https://learn.chatgpt.com/docs/automations)

插件入口与执行节点解耦；可共享插件目录不表示所有账户/Chat 模式都
已可用。先实际核对主账号、连接、工具调用和任务回执，再启动周期。
当前仅准备工作合同，**尚未创建或验收 ChatGPT 定时任务**。
[Plugin quickstart](https://developers.openai.com/plugins/quickstart)

## 当前续接位置

模型在同一项目 thread 中完成 v1 和功能增强 v2；随后集成包两端
941 项测试与公开 CI 通过，PAM-001 已发布；
隔离 VPS 上实际完成 epoch 1、2、3 的执行，owner 独立结果核验与
结算/容量释放均通过。PAM-003 发布为 7e569cf，同一冻结归档两端
1002 项完整测试与公开 CI 通过，隔离真实目录重联也通过。003d 已完成
空闲边界一致备份、旧源码保留与正式两向目录加载；继续观察真实数据，
不为测试破坏正在工作节点。PAM-002 的实际网络交接由原 Leader 和远端
specialist 完成，父 task 原 ID/原 thread 自动续接并汇总真实回执；PAM-007
新 worker 模块已加载。当时原生 plan checkpoint 未观察到，不假称已验收；
3–5 天 schedule 与运行期原生 goal 配置仍需实际核对，不用协调文件代替。
PAM-004a 已实现并正式加载，原 Leader 的实际试用 task 已 completed，
保留原 thread，实际创建并读回 waiting_evidence 决定。当前目录空、
两端旧采样过期，模型选择补 probe/executor 证据而非重复无效采样；
无新增 child/执行关联，不冒称最佳出口或业务成功。当时原 native plan/goal
checkpoint 仍未观察到，不把此 proposal 当替代；后续 plan 验收见本节末尾。
本次加载 cloud 原账本/线程行完全一致；laptop 断连时自动触发 episode 3
本地自维护，续接原 self-maintenance thread 后遇到 Codex 额度错误，
停在 needs_review。旧业务行保留，但原自维护 checkpoint 更新；不声称
所有行都未变，不重放该未知阶段、不恢复旧 DB 抹去它。下一片为
PAM-004b 一个真实出口及其独立 probe/owner 准入/真实 executor；
PAM-004b 已取得独立 Git oracle 与 VPS 单目标实际 GET（内容哈希匹配），
备用 profile 认证拒绝已通过同身份核对、备份与较新已有 OAuth 同步修复，
account-only 认证/额度通过。该 profile 的原 thread 历史是权威完整历史的
严格字节前缀；在原任务尚未进入 native turn 的静止边界保留完整旧字节
及一致 DB 备份，原路径原子刷新为完整历史，没有新建 thread 或恢复旧 DB。
原 Leader 原 ID 已在 epoch 28 completed，编写工具的 11 项离线测试通过；
Root 独立复跑通过，工具已 stage；随后已 publish、activate 安装绑定，
配置一个 managed request 的 owner 容量和独立新观测，尚未实际受管执行。
不把探测、认证恢复或 stage 替代模型选择和实际执行。
原 laptop 维护 turn 确有只读诊断 Shell 回执，不能套零效果 quota 重放；
保留 needs_review，后续使用显式效果对账合同。
范围见 [EGRESS-CANARY](EGRESS-CANARY.md)。PAM-006 先 owner-bound
ingress 后受限成果和 MCP/Cloud，详见任务记录；定时任务仍待实际配置。
Worker 失败阶段诊断新增 20 项测试，相关 98 项回归通过；仅诊断，不改变
重试/效果 guard 或自动覆盖原生历史。首份冻结代码两端 1111 项各有一个
固定无本地后端错误被泛化的回归；已修复并保留原测试断言，旧归档未部署。
当前并行片为 Firefox 手动入口（28 Python + 19 JS 离线合同）、Cloud
持久单次 job adapter（32 离线测试）和薄原生 Codex MCP/plugin（32 离线
测试，按已装 0.162 的 legacy stdio 协商）。源码 `7fb001a` 已公开发布，
CI 37960801403 completed/success；本机完整 1240 项通过（149.351 秒）。
VPS 隔离 1240 项初次因两处测试使用 Python 3.6 不支持的 subprocess
参数失败，兼容性修复后完整 1240 项通过（140.831 秒），未改系统 Python。
此前正式核心为 cf308b3；随后 `e2397f1` 已公开并两端加载，本机
1267 项（144.987 秒）、VPS Python 3.6 1267 项（141.601 秒）完整通过，
CI 38007498893 的 3.8/3.12/3.14 实际 success。同一冻结归档 SHA-256
`e40591141bfdb2a6eafdb96dea56ebf6c5c8ec13589e189f38d20edee688298b`。
升级只更换源码，当前配置、原任务/原生会话/历史块/执行账本指纹保持；
新增 continuation 表为空，不恢复 DB/history，不重启隧道或 cloud companion。
私有部署检查初版整表序列化在 896 MB VPS 上触发 OOM，发生在意图/停机前；
逐行流式版实机峰值约 19 MB 后部署成功，未扩容或修改系统 Python。
本机两条 epoch 0 的维护队列由原 needs_review 的同 scope 效果 guard 阻止
领取；所有原行/guard 均保留，部署静止检查不把这种队列当执行中，也不
把它当可重放业务。仍需按原维护 task 做 owner 效果审查。
Codex MCP 的实际安装/三工具发现、提交、原 Leader completed epoch 1、
Root 审查摘要发布及原 request 批准成果读回已通过；Cloud 仍无实际 job/node。
Firefox 的原 request
`mesh-firefox-05d0bce8d4728cd553d831b9317529e3` 已绑定真实任务
`ingress-c3d8bbe11285b26aab751c96b9dd8fe19229aea0bdb78900f6becf44c8799b76`；
保留它们和原 Leader thread。维护前只读诊断确认权威完整历史 9964417 字节，
当时候选 0 的目标 9160820 字节是其严格旧前缀；候选 1 持有相同最新
历史。历史观察中 epoch 5 的 `native_start_attempted=false` 只说明这一轮未
调用 start，不证明所有 epoch 零效果，不能清效果标记或换 ID 重放。
维护首轮因 Python 3.6 不支持 backup() 在 history publish 前失败，worker
已恢复 active、历史未改；随后一致 read-transaction SQL dump 备份和完整
旧字节保留后维护成功，仅停止/启动该 worker，其他服务保留。
原 browser task 已实际在原 thread `01a116fc-8aae-7001-a0e2-07a1073c5bcb`
completed epoch 16、turn `01a1217d-f3e4-7282-878b-5c4e44a15173`。
Root 独立 review 后发布 `PAM-006b-firefox-result-20261010-v1`，未转发
原 native result；Firefox 原 ID 状态及批准成果的真实回查、精确绑定已核验。
该成果仍不证明能力执行或引用内容/账户认证。实际能力目录/remote projection
为空、PAM-004b 仅 stage 是这份原成果当时的快照。后续新观测与容量已登记，
原 SELECT task completed epoch 1，写入/读回 `PAM-004b-canary-evidence-review-v1`
waiting_evidence 决定，未预留原 operation。模型指出旧原生 thread 无新增
allocation 动态工具，且将空 execution-candidate 段视为缺少 executor；该段
实际是已有 allocation/delegation 关联，并非安装目录。保持旧决定和原 task，
补当前 owner 安装证据、带当前任务 flags 的 CLI，以及 owner 显式 continuation
CAS；之后再由原 Leader 同 task/thread 选择、等待原 operation 执行和结算。
当前 CLI/worker 参考小片和 owner continuation 已完成并冻结；CLI 10 项、
continuation 16 项及相关回归通过，并已包含在上述两端正式版本。
continuation 保留旧完整字节和同 native source，以 operator/result/epoch CAS
仅追加新 instruction；子孙 hold/unknown 不因续接释放。
不以清效果、新 task/thread 或修改旧决定绕过此缺项。
新独立观测 `PAM-004b-egress-probe-20261010T0020-review-v2` 的实际 sample
time 为 1791591376.2591817，GET body 与独立 Git oracle 匹配，子进程 reap；
authority 已登记 verified。它按 900 秒失效，不以观测 ID 文本充当采样时刻。
owner continuation 已接受原 SELECT 的 completed epoch 1/result SHA，新工作
在原 task/Leader thread 的 epoch 2/新 native turn 运行并 completed；旧完整
结果在私有 journal 保留。模型真实选择 `PAM-004b-cloud-canary-select-v2`，
提及当前独立观测/容量/安装证据和 laptop 缺少可准入候选，保留旧决定；
读回确认关联原 operation。Root 单次 owner executor 实际 GET 后，原 operation
completed、dispatch 结算、容量 held=0/remaining=1；Root 和 Leader 各自核对
真实产物 raw SHA/内容长度/正文 SHA 与独立 Git oracle 匹配。
原始产物 SHA 为 `9cfcb6fd07da6e325994163fbea8c0b381ec40cd52e8644b2bfab263eb3f8c89`，
canonical artifact SHA 与保留 oracle 一致。Core 仍为 provider_reported，
不把 owner 内容核验升级为物理/性能证明。004b 的固定目标 canary 已验收；
004c 将日常 executor 交给 Mesh 而非 Root 逐次启动，并继续多候选和故障观察。
现已保留原 canary 闭合快照并加载独立 cloud provider service：enabled/
active、空闲报告 ok，原journal/canary行未变，其他服务未重启。原 pool
过期后以 owner epoch CAS 更新相同容量合同/原binding，新独立采样另存。
新的明确 `PAM-004c-executor-handoff-20261010-v1` 已在原 Leader thread
epoch 1 completed；模型选择/原新operation与service自动结算均读回，Root
独立artifact/Git oracle核对及容量归还通过，原canary行保留；Root未手动
execute。后续多候选/故障对账仍推进，见 [独立executor](MANAGED-EXECUTOR.md)。
该 004c trial 的原生 goal/plan 为 null；这项历史观察保留，后续 007 的
新 plan trial 不改写旧 checkpoint。周期检查未创建。
官方 Goals 合同明确 active goal 可在 idle 边界自动产生后续 turn；当时
adapter 只等待原 turn 的结束，因此先接入原生 plan 事件、自动 turn 的
同任务租约/效果生命周期，再实际启用长期 goal。不能在 completed 原
Leader 直接 set active，也不为 defer 标志 fork 新 Leader。
[Codex Goals](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex)
随后先实现了当前thread/turn的真实plan/goal通知观察候选，12项新fixtures
与69项相关回归通过，修正可选callback缺省后本机完整1279项通过；未从
旧plan推导本轮完成，当时未激活goal或声称已加载。004c实际完成但plan为null，
模型报告update_plan缺项；这不是对后来007 trial的结论。
详见 [native事件边界](NATIVE-PLAN-EVENTS.md)。

当前正式源码已是 `3198c877234a8fd0c6912fdbe218f47185effedc`：同一冻结
归档 SHA-256 `8362e05085179a06d2c9ff534df72a4460910c4f7ba97f696f2c52bf0b1436e6`
两端各完整 1282 项通过（laptop 146.487 秒；VPS Python 3.6 143.941 秒），
[CI 38020573081](https://github.com/gih10012/personal-assistant-mesh/actions/runs/38020573081)
实际 success。源码升级已在 laptop/VPS 完成，当前配置、原业务/原生会话/
历史/执行表与 laptop 两条 epoch 0 guarded 队列保留；未恢复 DB/history。
独立 provider 进程未重启。实际 cloud Codex 仍为 0.159.2；官方对应版本
源码确认原生 update_plan 缺省关闭，因此只给 Mesh child 加
`tools.update_plan.enabled=true`，未更换模型、预算、全局 profile 或权限。

新 task `PAM-007-native-plan-handoff-20261010-v1` 已在 cloud completed
epoch 1，保留原 Leader thread `01a116fc-8aae-7001-a0e2-07a1073c5bcb`，
本轮 turn `01a123de-2b04-7d30-94b2-2bd8444a4b46`。Root 只读核对实际
authority：checkpoint plan 来自真实通知，threadId/turnId 精确对应本轮，
两个步骤均 completed；goal 为 null，side_effect_started 为 false，
runtime_failure 为 null，完整结果 SHA-256 为
`7914918df2e4abd37ce1642b637417efa8a2e698d70ae26b543f7e6ad18de48a`。
这是原生 plan 的运行期验收，不是长期 goal 自动推进、定时检查或模型
自主选型已实现。随后已实现同 task/epoch 的 goal 自动 turn、租约与效果
生命周期源码候选，34 项 adapter 与 12 项 Worker/Store 新 fixtures、
105 项 native 相关回归通过；尚未正式加载或在原 Leader 启用长期 goal。
停止/回收运行器后才清标记并上传原生历史，未知/迟到工作不重放，
authority 等子任务不再制造零效果。`ef647c80253740b1ac2e43b3e8e41df88e75afaa`
同一冻结归档 `dcc479462be8a07cb9d33590621a247bda34422832c69e1a4a746befb6635e64`
两端完整 1328 项通过（laptop 145.661 秒，VPS Python 3.6 142.574 秒），
[CI 38022301906](https://github.com/gih10012/personal-assistant-mesh/actions/runs/38022301906)
实际 success；候选未正式加载。上一份 `59bf944` 两端均有 5 个旧 fixture
错误、CI 失败，未部署；仅更新其旧 mock/断言以符合 actual goal 读取，
没有放松运行期检查。next task 为 007c 安全 active 协同 yield，007d 连续原生 rollout checkpoint，
007e 实际周期与模型选择；这些是协调 task，不冒称已提交 runtime。
当前 active goal 请求等子任务仍显式报告未支持并保留 unknown；途中
跨节点记忆复制与强实时 lease watchdog 未完成。Chat 原生 MCP/OAuth
未接通，Cloud 本项目环境仍为空且无实际 job/node。
官方 socket graceful drain 的私有阶段一已实际通过：真实测试 turn 自然
结束、运行器退出码 0，同 goal 仍 active、原生字节前缀保留。阶段二
同线程 resume 没有重设 goal/turn/start，但未进入新输入握手，随后
强杀专用 PID；实际 6 started/5 completed，末轮保留 unknown，不重放。
这是新的实现依据而非正式 Worker yield 验收；下一片需显式一次性交付
新 task 输入、失败先 drain 与独立 yielded outcome。stdio 不支持这一
排空，unsubscribe 不阻止 goal 自动续跑，不能借新线程绕过失败。
详见 [原生socket实测](NATIVE-SOCKET-DRAIN-TRIAL.md)。
下一片源码已补可选私有Unix WebSocket、resume输入的持久intent/hash
与实际turnId回执、host回调握手边界、子task不继承父goal、yielded
scope等待/唤醒/零native-start的auth/backend回退预留。缺少/重置的
原生goal状态不从配置重新创建。当前仍未实现active yield的独立seal，
也未启用正式目标。`455430d` 同一冻结归档
`486f84c1b00e53e27b66ec938969ddce634717104b8dd312196be587da805472`
两端完整 1424 项通过（laptop 144.480 秒，VPS Python 3.6 144.367 秒），
[CI 38029201527](https://github.com/gih10012/personal-assistant-mesh/actions/runs/38029201527)
实际 success，源码已公开。该版本实机 idle 检查在两端都因官方
rendezvous symlink 被拒绝，未 initialize、更没有 model/thread/goal 调用。
随后按已读官方 0.159.2/0.162 listener 修正精确 alias 与物理目标校验，
握手前核对 owned app-server PID/UID，不放松通用 symlink 校验。
本机直接官方 native binary 的零模型初始化、空 loaded list 和自然
退出已通过；npm 包装器 PID 不同则拒绝，不能猜子孙进程放宽。
随后 `ee6f924c3d74346683f9750caee7ef06586606c7` 已公开，同一冻结归档
`8fd8b9ab04d4e54e0009e6dc542e6d07d822870687ce21be02623f21d45b07f4`
两端完整 1449 项通过（laptop 147.346 秒，VPS Python 3.6 142.501 秒），
[CI 38030126126](https://github.com/gih10012/personal-assistant-mesh/actions/runs/38030126126)
实际 success。冻结代码在本机 native 0.162 和 VPS native 0.159.2 均
实际 initialize、loaded list 空、自然退出码 0、reader 回收，无强杀，
没有 model/goal/resume 调用。两端正式 core 的 PID/NRestarts/代码哈希
仍是旧冻结版本，未部署候选、未改配置或恢复原失败工作。
这完成 `PAM-007c-1` 源码候选验收，不是 active goal 交接已完成。

后续 `PAM-007c-2` 已实现默认关闭的 `native_goal_drain`：核验 owned
Unix peer、先 gate/持久原 epoch intent ACK、一次 owned PID TERM、
持续 heartbeat 的 admitted event pump。自然 completed/idle/0/reap/EOF
只得出 drained_unverified，Worker 禁止 post-close RPC，保持原 guard。
23 drain 与 6 Worker 新 fixture、原生近邻238项通过；新 adapter 的
active 模型实测和独立 yielded seal 尚未验收，正式配置未启用。
`PAM-007c-3` 的可选 frozen-file upload 前置亦已实现（33新fixture），
默认保存行为不变；不把文件 SHA 当作原生 goal 迁移证明。
首版 e229651 两端1508项通过但 opt-in idle 实机失败，不列成功；
pre-start 全局通知/tracker 修正后的冻结 `58fac34` 已在本机/VPS
Python3.6.8各通过1510项（138.158s/142.777s），公共 CI38038923724
实际success；同一源码 opt-in idle 在native0.162/0.159.2实际
owned-peer initialize、loaded list空、自然0及reader回收，无强杀。
候选未部署、drain默认false、严格fingerprint默认None；正式核心
PID/NRestarts/原codex.py哈希保持3198c87。新 adapter 未启动模型或
active goal，不能把此验收当真实active drain或父子交接已完成。
此前下一切片按固定 task ID 推进：`007c-3` 自然回收后的独立同 goal/用量/
turns/历史核验与独立 yielded seal；`007c-4` 原父 task 等待/唤醒、新 epoch
同 thread/goal 输入真实往返。未核验不能清 guard，stdio 仍明确未支持。
详见 [输入与归属前置合同](NATIVE-INPUT-HANDOFF.md)。
详见 [native事件边界](NATIVE-PLAN-EVENTS.md)。

当前 `007c-3` 与 `007c-4` Worker 接入已实现默认关闭的源码：实际 goal
baseline、同文件有限前缀、独立 readonly observer 的完整分页/同目标
预算和累计用量、不加载原 thread、自身自然退出、稳定完整 JSONL。
父 task/epoch 的 guard 在冻结历史 upload/commit 明确 ACK 之前保持；
自然终态优先，不重设 goal/用量，不发 post-close RPC。新增40 history、
19 observer、7 Codex seal、14 Worker、1 Store ACK fixtures（共81项）；
双端冻结验证、CI与新受控实际 canary分别记录，不能以 fixtures冒充运行验收。
正式配置仍未启用，原私有 unknown 保留。下一步为新受控 adapter drain/
independent seal，再原父 task/同 goal 等待唤醒的真实 roundtrip；Chat 原生
tools/OAuth 与 Codex Cloud job/node 仍未入网。

随后 `85f9611` 同一冻结归档
`13f3f6038662bf13699e172dd097ceeb1644a9718ace637636a339343af164ac`
两端完整1591项通过（本机148.597s、VPS Python3.6.8 144.798s），
[CI 38043492654](https://github.com/gih10012/personal-assistant-mesh/actions/runs/38043492654)
实际success；两端独立observer零模型启动/自然0也通过。新的私有
tool-field合同canary在本机原生0.162真实active goal下自然drain、独立
observer/history seal及strict artifact ACK通过，同目标/createdAt/累计
用量保留。这一首epoch用实际Codex及Worker validator，不冒称full Worker。
子任务完成后，原父epoch2的实际full Worker同thread/goal resume一次、
steer一次并再次严格seal，自然0且guard最终false；模型再次等待，未输出
真实child marker，因此结果消费尚未验收。当前修正额外Mesh wait能力，
按同租约事务的实际终态返回False/完整结果，旧回执不可变、TERM后不撤销
已接纳等待；19项新fixtures与107项回归通过。冻结验证后只续接这条
安全pending原任务，不重放任何unknown。正式服务仍为3198c87。

随后wait-ready源码 `79d8931` 已公开，本机冻结1610项完整通过；VPS
Python3.6及CI Python3.8有一个HTTPError空fp测试cleanup错误，修正为
空BytesIO且原断言不变。原父epoch3的实际False回执/原child结果在
持久journal和原生final均已独立核对，模型确实消费；但goal未终结，
自动重复直到专用canary timeout。原guard/unknown保留，不能再次续接。
只读审计确认同active goal、完整原prefix与64个自然完成turn，原authority
行未变，仍不冒称Worker完整terminal交接。下一步核对官方原生goal
模型工具开放，新能力合同另验；正式服务不加载候选、不强设complete。

夹具修正后 `d8c603d` 冻结双端各1610项完整通过（146.256s/145.782s），
CI38045193255实际success。并行源码核对排除了goal功能默认关闭，确认
真实get/create/update工具及Code Mode的tools.get_goal/tools.update_goal
名称；模型tool_mode差异不能靠直接schema猜测。新实际目录模型sol
无active只读get_goal通过，另一个独立合同的原生get/update/同goal
complete和普通saved artifact核对通过；私有客户端漏最后children查询，
完整Worker task未通过并保持unknown，不重跑。不同模型/合同的局部
证据不能拼成C4完整往返。DEFAULT等待布尔及WORKING_CONTRACT原生工具
提示已同步，两个新fixture/151项近邻通过。冻结后下一步使用完整真实
API客户端验父子同goal合同，而非不断手写不完整的私有假客户端；正式
环境、所有旧失败身份与guard保持，Chat/Cloud仍未真正入网。

正式 laptop worker/node 原先直接以开发仓库为工作目录，重启可能误载
候选。已保留一致账本与单位配置，改为私有冻结 `3198c87` 版本目录；
实际两进程 working directory 已核对，原行、配置字节与两条 guard 未变，
未加载新 goal 控制器、未启用目标，也未恢复 DB/history。独立 provider
未重启。native-recovery 仍在旧进程/开发目录，其他 CLI/MCP 入口尚未
全部隔离，不把两个核心单位的切换说成全面版本隔离。
随后已备份并固定 Firefox host 与既有 Codex MCP 的新启动路径；实际
原请求 status/result、task/publication 及完整批准投影保持，身份配置
不变，没有新 task/model 或既有进程重启。存活旧 MCP/恢复进程和模板
仍单列，协议客户端的读回不等于 Chat 模型或浏览器 UI 新验收。
续接 PAM-007f 的 [运行发布边界](RUNTIME-RELEASE-BOUNDARY.md)。
Cloud 本项目环境连接/发布条件单列，不以此缺项阻断 004b 中层真实出口。
不把这组工具验收当作去中心化、完整自主授权或整个 goal 的完成证据。

2026-10-10 最新运行代码候选 `a4c412a` 已冻结发布：归档 SHA-256
`d50043d82bfbe1f780bba802e9d2be6f555ba3748c7175863c9c898f14478cd8`，
laptop 1612 项完整通过（148.361s），VPS Python 3.6 1612 项完整通过
（142.984s），CI38046321351 completed/success。默认 wait 布尔提示与
真实 code-mode goal 工具名称已同步；完整父子 HTTP 合同实测尚未启动。
不把此前不同 canary 的局部证据拼成完整交接。正式两端仍 3198c87，
drain/yield 为 false，本次只读确认核心 active、双向 A2A connected。
ClawBot 仍 VPS 单后端，校园可调用出口不存在；Chat 原生/官方 Cloud
未入网。这些缺口已进入上述并行优先项，不再全部等待 007 完成。
