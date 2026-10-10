# PAM-004b：真实出口的第一条能力链

2026-10-10，状态是**安装绑定、容量与新独立观测已登记，模型选择执行仍在推进**。
这不是固定网络评分器，也不是 agent 原生联网的准入门禁。

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
实际 publish 和 manifest activation 已通过，安装绑定当前一致；runtime-loaded
和实际受管 callback 执行仍未核验。owner 配置一个 `managed_gets` 容量、
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
证据与 task-bound CLI、显式同 task continuation 后再由模型决策；不改
旧 proposal 或借新 effect/thread ID 重做。旧原生权限持续开放。

实际 laptop 认证 projection 查询已看到 cloud 的这一能力且 `available=true`；
这证明非空目录传播，不等于跨主体 grant、远端执行或全局一致性。
原生工具创建已发生，不把认证恢复、测试或 stage 当作模型选择/
业务执行。诊断与保留完整历史的恢复边界见
[NATIVE-HISTORY-RECOVERY](NATIVE-HISTORY-RECOVERY.md)。

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
下一步是 owner 准入、fresh 独立观测和原 Leader 真实选择/执行，整体 goal active。
