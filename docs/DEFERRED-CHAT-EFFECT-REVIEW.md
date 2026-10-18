# 独立的 owner-deferred Chat 已知效果结案候选

叶任务由 `confirmed_profile_binding` 负责，分支
`work/deferred-chat-effect-review-20261017`，base `d5d3a3b`。
仅供 Root 审查和单线操作；本说明、合成检查和私有原命令清单都不是
生产结案证据。没有运行生产 apply、临时恢复 VPS 数据库、改认证或重放 Chat。

## 为什么不直接调用现成 API

原 Store 的 `claim` 会检查同 scope 其他任务的 `side_effect_started`，
所以仅改成 failed 仍会挡新工作。普通 update/control 不能核定
needs_review 的已知外部效果；zero-effect quota 工具既拒绝这次非零
效果，又会将原输入 pending，违反本人已延期主动 Chat 的要求。
旧 `review_native_effects.py` 的另一个 closed case 不能复用或放宽。

`scripts/review_deferred_chat.py` 是**另一个且仅一个已独立审查历史 case**。
opaque code pins 锁住原 task/session、完整 18,632,493B rollout、267,291B
选中轮次、既有 backup、私有 payload 和原 queued/send_intent0 行。
选中片段包含全部原 JS 调用、shell、输出、状态/计划和 quota 终态；
它是具体已审原字节集合，不是通用 Shell/JS 审计器。私有 spec 无法
通过 repin 接纳未来任务/命令；旧 closed-case 实现/manifest 完全不变。

完整私有命令和输出由 Root 从本机私有审计交接阅读，不进公开仓库。
精确计数是9个 shell CommandExecution、9个 JS exec envelope，其中
最后一个 envelope 只观察原 handle，另一个含两个 shell；并非9次
独立网络请求。所有 shell 有终态，原两个 exit1 保留为失败而非成功。
已知写入包括本机私有临时目录/payload 和原 Chat queue，不能说零效果。

## 必需的当前闭合证据

由固定、只读 SSH observer 检查原已授权 peer；不发模型/Chat
请求，不读账号 token，不消费任何增量游标。原 owner config 必须是
当前 code-pinned disabled 字节，原 queue 行须完全一致且 queued/0，
原 payload 须完整字节匹配。原发送 host 的实际独占 lock 必须可取。
默认 audit 的短命 observer 只是采样，不是未来 CAS 的排他权；apply 使用
下述同一长连接 observer，持有真实 fd 覆盖最后检查、COMMIT 和状态读回。
不以某 PID 不存在、等待超时、模型 JSON 的“已闭合=true”或配置文件 false
单独作证。回应绑定新 nonce、递增 sequence、当前时间、同 PID/lock inode；
所有目标/摘要由代码锁定，结果只供私有审计。

Root 先前 TERM 过原 host，但2026-10-17初始只读采样时 **host.lock 仍持有**。
可能是浏览器重建了新的 disabled host，也可能存在旧缓存，尚未取得
足以接受的证据。本候选此时会拒绝，不能称生产 eligible。Root 应按
实际 host 协议处理/核实，不能为了通过而删 lock、绕检查或恢复旧发送。
若要用其他闭合证明，需另外独立审查 source，不接受手填 boolean。
Root在2026-10-18已按正常Firefox UI移除自己临时加载的旧协作扩展；本叶未
据此重读生产queue/lock或声明eligible，后续仍以真实持锁/原记录核验为准。

### P1 修正：同一真实锁覆盖 CAS/commit/readback

原候选 `1b4b6df` 的两次短命检查有竞态：原 host 先构造 Journal 并缓存
enabled，才取 `host.lock`。本叶在实际原18cf源码上用临时合成 queue 复现：
磁盘 disabled、短 observer 取锁成功并退出后，缓存 enabled 的原 native_host
仍可给同请求 `send_granted=true`。0真实 Chat/模型/原生产 queue 操作。
**原候选不可仅凭该瞬时证明 apply。**

修正使用唯一 owned SSH 子进程：先核 disabled 配置/原 queued0/payload，
持有原 `host.lock` 的同一 fd；随后只接受同 nonce、严格递增 sequence 的
check/release 帧。每次重核原文件和锁 inode，不能换新 observer 拼接证明。
完整 backup restore/audit仍在锁外。短锁内只做最后原状态 CAS、完整 artifact
写入和读回：SQLite 的实际 COMMIT authorizer 再向**同一持锁进程**取新回应，
检测终态、EOF、超时或身份变化就拒绝 COMMIT、rollback；不是前面检查过
布尔值就继续提交。commit 后用原 `--status` 的真实账本/完整原字节读回，
再 check，写07回执，发明确 release；要求相同 nonce/PID/序号 ACK及进程0，
最后才写 `08-peer-released.json`。

stdin/SSH EOF、普通终止信号和异常**不会自动释放已准入远端锁**。这样即使
最后 ACK 后链路失效，也不会把该间隙自动变成旧 cached host 的运行窗口。
这不是抵御远端进程被强杀/机器崩溃的分布式原子提交，也不证明未来任意
旧 host 永远不能再运行。Root仍须保持本人已经延期的旧扩展/host停用；
正常显式 release 是结束本次排他的 owner 决定，不是给旧队列新发送授权。
UI停扩展的观察不替代锁本身，锁释放也不改旧 queue/input。

异常后只查同 case：`06-cas-intent.json` 记录原 nonce，
`06-peer-held.json` 记录实际 peer PID/lock identity。锁可能仍在、SSH进程
也可能仍存活；不可删 lock 文件、凭超时/纯数字 PID盲杀，或新开 case重试。
需 Root核原 outcome、已持有的进程/连接身份及原 fd 后单线显式处理；本工具
不新增远程解锁/自动恢复平台。正常完成才有绑定的08释放回执。`--status`
分别报告 task CAS 是否已应用、`peer_lock_release_verified` 和
`peer_lock_may_remain_held`；DB已结案不等于释放回执已确认。明确 release 已
到达但 ACK 丢失时锁也可能已释放，仍不能假报确认或重做原任务。

数据库检查保留原 task/epoch/scope/thread/turn/result/session 的完整摘要，
同 scope 不得有运行任务或其他待审 guard，原 task 不得有 child 或已知
allocation/delegation/interaction/continuation 依赖。原 native state 不得
带未闭合 goal/lifecycle/guard。原旧 chunks 全字节核验并为当前 native
文件的严格前缀，历史不能倒退到只含旧 chunks 的快照。

## 复用既有一致备份，不再复制约300MB

使用 Root 已有的、原目标仍处相同待审状态时生成的一致 SQL gzip。
只读逐块核完整 compressed SHA/size，再以 gzip CRC/EOF 和每条完整 SQL
恢复到**新私有临时 SQLite**，不是 live ledger。要求完整 BEGIN→COMMIT、
完整 uncompressed SHA/size、quick_check、原 selected 行/旧 chunks 与当前
case 完全相同。复用已有 authorizer：禁 ATTACH/DETACH/PRAGMA、virtual
table 和 load_extension；完成后换显式只读 verification callback，兼容
Python3.6 的缺常量/不能 set_authorizer(None) 行为。不截短、不跳表，不
以模型或文件名“backup”代替验证，不把历史备份覆盖当前数据库。

与旧 case 的两倍 ledger-size 策略不同，本新候选明确采用**一个 live
ledger 大小的 max_page_count 硬上限 +32MiB工作余量 +1GiB最低空闲**。
这只限制新空白临时恢复库；它没有要保存的旧页/第二份旧 journal。
每条 SQL 后检查1GiB floor，不足或恢复库超 page cap 则失败并清理自己
的临时恢复目录，原 backup 不动。它不能保证任意 dump 都可压在当前
ledger 大小；压不下即失败，不自动扩 cap/删备份/扩磁盘。Root 可选择
另外审查的离机验证流程，但不能用 verified boolean 绕过当前 source。

已实核只读 gzip 的298,121,656B和解压 SQL 的528,766,045B完整 SHA；
并未在 VPS 做临时 SQLite restore。采样空闲1,423,278,080B，live ledger
269,266,944B、WAL0；这只是当时读数，不承诺未来操作时仍有相同空间。

## 显式 Root 审批、一次 CAS 和同身份 status

私有0600 spec 字段：protocol、issued_at/expires_at（不超过30分钟）、
worker_config、server_config、task_id、epoch、rollout_path、old_artifact、
backup_path、review_root、local_observer。local_observer 固定既有 SSH
executable/config/alias 与 targets（原 owner_config/queue/payload/request）。
包含原标识、私有路径与关联的 spec 不进公开仓库；Root 核真实 authority、
上述9条完整原命令和所有闭合证据后才批准其精确 SHA。没有自动 refresh
旧 spec、更换旧 case ID、复制 auth 或新增网络/模型调用。

Root 从审核后的 frozen source 执行默认 audit（会写自己的临时恢复库，
不改 live/task/history，原 backup 不变）：

```sh
python3 -m scripts.review_deferred_chat --spec-file /PRIVATE/owner-deferred.json
```

只有确认本次 eligible 后，Root 才明确加
`--apply --approve-spec-sha256 APPROVED_DIGEST`。该操作在新同 case 私有
目录保存00 intent/完整before、完整失败 rollout gzip；不制造第二份 SQL
backup。06 intent 先于唯一数据库事务。事务内重新核 task/session/rollout/
backup 和当前 peer closure，以原状态作 CAS，新增完整 immutable artifact，
原 chunks 不改；native_sessions 仅提升 artifact/updated，**state 原字节
不改**。原 task 保留 input/result/原 checkpoint 字段，只追加明确 owner
deferred/nonzero/requeue=false 审查，settle 此一个 guard，status failed、
epoch+1、node/deadline清空。append-only 独立审查表不修改旧05a0审查记录。

它不把旧输入 pending、不启动原 turn、不设置/恢复 Goal，也不修改原
native 文件、thread、账号或 Worker。新入口真正 handoff 的独立请求之后
是否被正常 claim/运行需 Root 从实际 authority 验证，不能仅凭旧 guard
结案就宣称新任务成功；旧默认 auth 的配置问题也不由此工具暗中修改。

有00 partial intent 或06/07后未知只运行原 spec 的 `--status`；没有自动
prepared-resume/reapply。丢 ACK 通过 append-only 原审查行、完整 before/
typed receipt、原 failed/new epoch task和完整历史字节读回，不新建case。
status 可读原 expired spec，不能据此授权 mutation；以后新工作正常推进
native session 不会被 status 恢复到历史状态。partial-before 若需续接，
必须再做单独 owner 审查，不删原文件后重跑。

## 必要验证

原8项合成 real-SQLite checks：完整已有 backup 临时恢复且非 live 改动；
精确 failed/deferred CAS、保留 input/result/native state/旧块；原字节改变
或截短 backup 拒绝；真实 authorizer 阻止 ATTACH；CAS race 留同 intent
unknown；丢 commit ACK 用原 status；固定 peer observer 对原 queue/disabled
config 和真实独占 lock 的检查。这里只替换测试代码内 synthetic case pins，
没有 CLI/spec admission override，也没有生产 apply/发送/认证/模型调用。
本次额外3项仅针对P1：真实 observer子进程持锁时原 cached-enabled host 在
CAS/commit/status后、显式release前仍被真实flock拒绝；observer在SQLite
COMMIT准入处死亡使原任务/guard完整回滚；stdin EOF不释放远端锁且不能
准入COMMIT。共11项通过，未加大套回归；测试中的强杀只清理已核结果的
自建合成子进程，不是生产自动释放策略。Python3.6语法核过；真实 VPS
restore/审批/CAS/新 handoff 均待 Root 实核。

本次不修改原 CLOSED task/session、18,632,493B/267,291B native bytes、
298,121,656B gzip和528,766,045B SQL pins。Root后续另取的完整本机备份是
独立证据，不因更大/更新就自动替换本 case 的已审备份，旧case/source不放宽。
