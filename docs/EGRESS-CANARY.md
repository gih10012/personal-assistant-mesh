# PAM-004b：真实出口的第一条能力链

2026-10-10，状态是**固定目标的模型选择、真实受管执行、独立产物核对和结算已通过**。
这不是固定网络评分器，也不是 agent 原生联网的准入门禁。
日常 executor 仍由 Root 单次启动；自主常驻执行与真实多候选调度由
PAM-004c 继续，不把此 canary 当作完整自主性。

## 实际证据

Root 从本人的本地 Git 对象读取公开提交
`796a1b3689686c9a591f9c8bf9ed3e548a2bd320` 的 `README.md`，
取得独立 oracle：10,814 bytes，原始 SHA-256
`00e502b82056d6f5e0e6eabd1b8505553a42d7b1769a393f1d89f85f258249c8`。
oracle 不来自 provider 返回值、自报或测试。

随后在 VPS 原生终端做了一次有界无认证 GET，固定目标是该提交的
公开 GitHub raw README。禁用代理环境注入与重定向，TLS 校验开启，
最多读取 65,536 bytes，不重试。实际请求 0.386 秒完成，内容字节数/
哈希与本地 Git 一致，checker 的子进程已 wait/reap。采样时间为
`2026-10-09T13:58:25Z`，它是历史样本，不因写到本文而续期。
checker 首版外层 watchdog 为 14 秒，后续收紧为 12 秒加最多 2 秒清理；
本次实际耗时小于 12 秒，不冒称首版所有失败路径均满足 12 秒总截止。

这只证明该目标/路径/时刻可达，不证明全互联网可达、物理带宽、最佳
出口、脱网推理或受管 callback 已执行。未改路由、代理、Wi-Fi、隧道
或服务，未创建付费资源。

## 原 Mesh Leader 任务

稳定 ID `PAM-004b-egress-author-20261009T134300Z` 已提交，要求沿用
原 Leader native thread/原生压缩，编写并离线测试单目标 GET 工具。
开始时主 profile 额度耗尽，备用 profile 的 account/read 被 401 拒绝，
任务 waiting_backend，无 native thread/turn checkpoint。

只读核对确认本机和云端备用 profile 身份指纹一致，本机有较新原生
OAuth 刷新结果。已备份旧认证并原子同步到**原云端认证目录**；未创建
密钥、重新登录、改账号池或重启服务。独立 account-only 检查确认
认证正常、额度 available；原 worker 自动重试**原 task ID**。

备用 profile 原 thread 历史为权威完整历史的严格字节前缀（205,696 对
9,160,820 bytes）。在原任务尚未启动 native turn、无写入者的边界，
保留完整旧历史、一致 SQLite 备份及完整源，原子发布完整原生字节至
同一规范路径；只停/启云 worker 一次，没有新 thread、摘要替代、
DB 恢复或效果标记清理。原任务随后在 epoch 28 completed，保留原
Leader thread；模型写出的工具及 11 项离线测试已由 Root 独立复跑。
源码 SHA-256 为
`982cf1027248a1682ee84eefcdd4798deddcbabdfe29b3e79088374a52b5dec8`。

工具曾为 `staged`，使用 stage 回读的新 descriptor。后续同 release
实际 publish 和 manifest activation 已通过，安装绑定当前一致；当时 runtime-loaded
和实际受管 callback 执行尚未核验，后续验收见下节。owner 配置一个 `managed_gets` 容量、
绑定同 capability epoch 1，并把已有 operator 作为独立 verifier；未共享
operator bearer、未加跨主体执行 grant。新一次单目标 probe 原始 body 与
独立 Git oracle 匹配、子进程已 reap，观测 ID 为
`PAM-004b-egress-probe-20261010T1700-review-v1`，实际 sample time
为 `1791589410.2001119`；它仍按采样时间失效，不因本文续期。

原 SELECT task 同 thread completed epoch 1，真实提议并读回
`PAM-004b-canary-evidence-review-v1`，选择 waiting_evidence，没有 allocation
或 provider execution journal。旧 thread 无新增 allocation dynamic tool；
模型还把空 `managed_execution_candidates` 视为未加载 executor，而该段实际
只列已有 allocation 关联，不是安装目录。下一步给出当前确切 owner 安装
证据与 task-bound CLI、显式同 task continuation 后再由模型决策；当时不改
旧 proposal 或借新 effect/thread ID 重做。旧原生权限持续开放。

实际 laptop 认证 projection 查询已看到 cloud 的这一能力且 `available=true`；
这证明非空目录传播，不等于跨主体 grant、远端执行或全局一致性。
原生工具创建已发生，不把认证恢复、测试或 stage 当作模型选择/
业务执行。诊断与保留完整历史的恢复边界见
[NATIVE-HISTORY-RECOVERY](NATIVE-HISTORY-RECOVERY.md)。

## 原 task / 原 Leader 的真实完成验收

源代码版本 `e2397f19e922fecb3480f70e408aced83d2dee5a` 已在 cloud/laptop
加载；同一归档两端完整 1267 项通过，公开 CI
[38007498893](https://github.com/gih10012/personal-assistant-mesh/actions/runs/38007498893)
成功。仅更换源码，配置字节、原任务/原生会话/完整历史块和执行表指纹
保留；未恢复旧 DB/history 或更换 Leader。

新的独立 observation 为 `PAM-004b-egress-probe-20261010T0020-review-v2`，
实际 sample time `1791591376.2591817`，一次 GET 耗时 0.327 秒，正文与
本地 Git oracle 匹配且子进程 reap。其 900 秒 TTL 从实际采样计算，
不会因本文、ID 或后续验收续期。

owner continuation `PAM-004b-egress-select-continuation-20261010-v1` 用
原 completed epoch 1/result SHA CAS 接受新 instruction，完整旧结果和
旧决定不改。原 SELECT `PAM-004b-egress-select-20261009T134300Z` 在
同一原 Leader thread `01a116fc-8aae-7001-a0e2-07a1073c5bcb` 的 epoch 2
运行并 completed，新 native turn 为
`01a1232b-e7dd-7d71-a0e8-6fdbdc0be309`。

模型实际创建 `PAM-004b-cloud-canary-select-v2`，选择 cloud 的确切
安装 capability/epoch/scope/workload，引用当时 fresh 独立观测、容量及
安装证据，指出 laptop 尚无可准入候选。读回确认决定不可变地关联原
operation `PAM-004b-egress-get-20261009T134300Z`；不是 Root 代为预留或
硬编码出口评分，也不证明存在多个真实合格候选或全局最优。

在模型实际 reserve/link 后，Root 对这一原 pending operation 单次
启动 owner executor。安装 callback 实际 GET：HTTP 200、10,814 bytes。
原 operation/dispatch completed 并结算，共享池 held=0、remaining=1。
闭合 provider journal 及实际 artifact 已由 Root 独立读取，正文哈希/
长度与原 Git oracle 匹配；Leader 随后也核对并汇总原 task。

| 核验对象 | SHA-256 |
| --- | --- |
| HTTP 正文原字节 | `00e502b82056d6f5e0e6eabd1b8505553a42d7b1769a393f1d89f85f258249c8` |
| 实际 JSON artifact 原字节 | `9cfcb6fd07da6e325994163fbea8c0b381ec40cd52e8644b2bfab263eb3f8c89` |
| canonical JSON artifact | `085eb96c8165c21f6bac5de75bba44c86227ff6b6d5a80872c9644659ceb325f` |
| SELECT epoch 2 完整结果 | `8013c7f1a287bbe0929a2b7ccc1fb6c3b9c0c9ed23eb53bf17638818940d7f90` |

原字节 artifact 和 canonical hash 是不同核验对象，不能混用。
Core 执行证据仍为 `provider_reported`；owner 对此固定产物的核对不把
它升级成独立性能、物理主机或 quiescence 证明。没有重放 callback、
改原 operation 身份、扩大目标 URL、替换 journal 或新增跨主体 grant。

PAM-004b 的此有界 canary 已完成；PAM-004c 继续把日常受管 executor
交给 Mesh。原 native goal/plan 仍 null，PAM-007 继续衔接 goal 自动续跑
与 Mesh 租约。Chat 原生 MCP/OAuth、Cloud job/node 均未因此接通。

## 复用现有底座的验收链

1. 模型用原生工具编写/离线测试 callback；Root 独立审查。
2. stage 保留版本；使用 stage 回读的 descriptor，安装位置改变后
   不能沿用作者目录的路径哈希。
3. 配置独立 verifier，再登记 fresh、exact scope/workload、单位/来源
   完整的实际网络观测。Git hash 本身不是网络探测。
4. owner 定义受管容量池与 binding；这是受管并发合同，不是物理带宽
   或原生联网隔离。共用 NIC 的两个 GET 不是同一次 physical use，
   不能借相同 usage_key 只计一份需求。
5. 原 Leader 在 live task/epoch 下自主 proposal、reserve、link，callback
   启动前重新核对任务/Leader/能力/观测/池；不能提前结束父任务。
6. 只取 authenticated pending 中原 operation/capability 执行；普通
   provider poll 会消费所有安装绑定的 pending，不是单 ID executor。
7. 结果 artifact 用 Git oracle 可预测的确定性 JSON，耗时放 evidence；
   result_reference 绑定 `sha256:<artifact 原始字节 SHA>`，不能只用路径。
8. 校验整个闭合 journal（无 WAL/SHM、完整 operation 集合）、artifact、
   settlement 和容量释放。unknown 不换 ID/换 journal 重放；observer
   不独立证明物理主机、性能或 quiescence。

首条云端 canary 的 requester/provider 同为 node:cloud，准入走
same-principal；即使记录 scoped grant，也不能声称跨主体 grant 分支
已实测。后续另做真实 laptop 请求者业务，不修改此操作身份或借原
cloud fence 的 child 冒充 laptop。

## 笔记本维护效果对账

原 turn 实际 CommandExecution completed、exit_code=0，为有限服务/
日志/配置路径/SQLite SELECT 诊断，未显式重启服务、改联网或写业务
记录。退出 0 主要证明最后诊断成功，不等于整体维护完成。supervisor
identity_verified 早于命令回执，不能归因于 agent 修复。
随后 native turn 遇 usage_limit_exceeded；原 task 仍 needs_review、
epoch 1、side_effect_started=true，原 task/thread/turn 保留。

存在真实 Shell 回执，就不能套“额度拒绝且零执行”的 quota apply，
不能清标志、切账号/新任务重放。新的只读状态（unit active、A2A
connected）不替代原效果审查，也未证明 agent 修复或离线推理。
笔记本原 needs_review 保留，按原 task 做 owner 效果审查；两条同 scope
且从未领取的维护队列仍被原效果 guard 阻止领取，不清 guard 或换任务
重放。此 canary 不处理该原未知效果，整体 goal active。
