# Native history failure diagnosis and operator recovery

`checkpoint.runtime_failure` 是 Worker 最近一次观察到的失败诊断，不是
新的原生工具门禁、恢复授权或执行账本。它只包含固定分类和阶段：

| 字段 | 含义 |
| --- | --- |
| `phase` | `backend_preflight`、`native_restore`、`native_start`、`native_finish`、`session_save`、`coordination` 或 `task_finalize` |
| `category` | 已知的固定本地错误类别；未识别异常为 `worker_unavailable` |
| `attempt_epoch` | 发生该次失败的原 task epoch，不能当作当前重试的 epoch |
| `native_start_attempted` | 本次是否已经调用 harness 的 `start()`；不是原生 `turn/start` 已接收或未接收的证明 |
| `outcome` | `unknown`；报告本身不核验原生或外部效果 |
| `automatic_history_overwrite` | 本诊断不自动覆盖原生历史 |
| `retry_authorized_by_report` | 本诊断不授予重试权 |
| `recovery_policy_changed` | 本诊断不修改现有领取、等待、续接或效果审查策略 |
| `native_tools_intercepted` | 本诊断不拦截原生功能 |

最近失败可在之后成功的 task checkpoint 中保留。必须结合实际 task
状态、当前 epoch、原生会话和回执阅读；不能将旧诊断当作当前失败。
现有 Worker/Store 对起效前的 `waiting_backend` 等状态仍有自动续接
策略，不应把本报告误读为“所有任务均不会自动重试”。

报告不包含 exception 原文、路径、OAuth/账号、rollout、prompt 或
模型内容。已知 restore 分类采用精确白名单，如
`native_session_destination_conflict`、`native_session_thread_mismatch`、
`native_session_metadata_invalid`；未知异常和带额外私有内容的字符串
不反射到结果。已有固定 Codex/Pi 错误类别保持可诊断。

## 先区分失败发生的位置

`native_restore` 的 destination conflict 表示恢复的完整字节与已有
目标不一致。它不是网络失败，也不是应当删除旧文件、换 thread 或
摘要覆盖的理由。backend 已选中也不表示 native history 已恢复。

`native_start_attempted=false` 只说明本次未调用 `start()`。原 task
可能已有未知效果，原 `side_effect_started` 不清除。`start()` 一经
调用，在返回 checkpoint 前也可能发出原生 RPC；超时、丢回复、缺少
新 turn ID 都不能证明没有执行。原任务、thread、turn、operation 和
原效果标记必须保留，不能通过账号/模型/节点切换绕过。

`native_finish`、`session_save`、后续 coordination 或 finalize 失败
也不是“从头再做”的证据。先核对原 turn 及已保存结果/提交回执；
报告的记录请求失败时，可能未持久化诊断，不能因此重做原生动作。

官方原生生命周期区分 thread/resume 与 turn/start，turn/completed
才给出原生最终状态。[Codex App Server lifecycle](https://learn.chatgpt.com/docs/app-server#lifecycle-overview)
完整原生历史和压缩继续由 Codex 管理，Mesh 不编写应用级替代摘要。

## 手动核验严格前缀后刷新历史

以下是 owner 已授权维护中的人工/独立审查流程，不是自动恢复脚本。
不要将它套用于 `needs_review` 的未知效果清理，或用它授权新 turn。

1. 从原 authority task/session checkpoint 确定原 thread、harness、
   原 artifact 及已授权目标。拒绝由待恢复文件自行选择 thread、
   任意路径或新账号；不要复制、打印凭据。
2. 在准确的维护范围内建立静止边界：同一 thread 无在途 turn、
   无领取/保存/恢复者并发写入，检查相关进程和实际原生状态。
   仅检查 PID 或文件锁不够；不停止其它正在工作的终端/任务。
3. 将源完整历史、目标已有完整历史、相关原 checkpoint/引用及
   恢复前元数据备份到仓库外本人所有的私有目录，文件保持 0600。
   记录完整文件 SHA-256、大小和版本元数据；备份不是摘要。
4. 核对完整 session metadata 中同一个原 thread 和规范目标，源
   artifact 与 authority 记录相符；检查文件为安全的自有普通文件，
   不跟随 symlink。完整下载、解压及完整 JSONL 行必须得到验证。
   若原生格式依赖分页或其它侧文件，还须核验对应原生依赖完整；
   单个文件的完整哈希不自动证明全部模型上下文已恢复。
5. 用实际完整字节判断，而不是行数、mtime、模型自报或 UI 摘要：
   源/目标完全相同则无需替换。只在目标是源的**严格字节前缀**，
   且两者记录完整、相应 turn 已终结时，才有“目标是旧快照”的证据。
   还要独立确认源是原会话已核验的最新历史，而不是旁支或旧归档。
6. 写入前再检查源、目标和 authority 引用均未改变；保持同一原
   thread，按本次明确维护授权原子发布已核验的**完整源字节**。
   保留已有完整目标备份，不恢复旧任务 DB、不清除效果标记。
7. 核对发布后的完整哈希、原身份和 authority 当前引用；仅在原
   task 的正常续接合同允许时继续。恢复文件不证明推理、Shell、
   网络或业务已完成，需观察该原任务的实际原生回执。

目标更长/更新、内容分叉、前缀不匹配、文件改变、源不完整、在途或
未终结历史，均禁止自动覆盖或合并；保留完整证据并单独对账。
不能截掉新 turn、删去工具记录、拼接历史、创建替代 thread 或把模型
摘要当作完整原生上下文。静止和严格前缀无法确认时保持冲突，不猜测。

当前代码仅增加诊断，没有实现以上手动刷新自动化。离线测试覆盖
restore 冲突不启动、启动丢回复仍未知、原身份/效果标记保留、各失败
阶段、私有异常不外泄；不是某个实际账号或历史刷新成功的验收。

## 下一片：原生写锁保护的旧前缀自治恢复（未实现）

2026-10-10 本机 `/usr/bin/codex --version` 实测为 0.162.0；以下为同
`rust-v0.162.0` 官方源码审查，**不是已部署恢复功能或实际锁互操作测试**。
当前 `_publish_selected()` 仍拒绝任何不同 hash 的已有目标，未默认放开
guard，尚未定义或实现新的 authority CAS 接口。

原生 [WriterLockCoordinator](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/rollout/src/writer_lock.rs)
已提供同一 Codex home 内的跨进程写锁：`thread-writer-locks/` 下的
`.coordination.lock` 与 `<thread_id>.lock`。原生 writer 先持 coordination
再拿线程锁；publication 在持 coordination 时探测线程锁空闲，阻止新
writer 在检查与发布之间进入。后台 recorder 持有线程锁直到文件工作
完成。[LocalThreadStore resume](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/thread-store/src/local/live_writer.rs)
实际使用该锁，并在 writer ownership 下核验 `history_revision`，变化则
重新加载。Paginated JSONL 是 canonical，SQLite 是可重建且可能落后的
projection；不以复制或回退整个 Codex SQLite 修复恢复问题。

官方 [App Server 文档](https://learn.chatgpt.com/docs/app-server) 此次读取仍
称 paginated resume 不支持；同 tag 的
[thread processor](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/app-server/src/request_processors/thread_processor.rs)
已经实现 paginated resume/pagination。应按实际安装版本核验，不把滞后
文档或 main 分支当运行保证。`excludeTurns: true` 省略响应中的 UI turns，
不是删掉模型历史；协议的 `thread/resume.history` 仍标注 Cloud 专用、
不应使用，不能以 Responses items 注入替代原线程的完整历史运输。
[ThreadResumeParams](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/app-server-protocol/src/protocol/v2/thread.rs)

最小实现只面向已核验版本和已授权 homes 的自包含单 rollout root
历史：完整 JSONL、完整源/目标字节、同一原 thread，`history_base` 为空，
无 revert 多 rollout、缺失依赖或未知格式。单个 session metadata JSON、
`thread/read(includeTurns=false)` 响应、文件 hash 或模型摘要均**不是完整
native 上下文**。原生
[rollout lineage](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/thread-store/src/local/rollout_lineage.rs)
会追踪祖先范围；
[current rollout resolver](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/thread-store/src/local/thread_rollout_resolver.rs)
表明 revert 可保持 thread ID 却改变 rollout ID/选中路径。复杂 lineage、
外置附件和压缩表示须另做完整 manifest/原生运输，不套单文件刷新。

建议后续顺序：

1. Authority 固定原 task/epoch、scope、harness、thread、selected artifact
   revision 与完整内容 hash；原 lease/Leader term 和原效果 guard 仍生效。
   未决效果保持原身份和审查状态，历史维护不授权业务重放。
2. 完整下载并验证，准备仓库外 0600 的完整源、完整旧目标和恢复 journal。
   对准确 homes 按固定顺序取得原生 coordination，非阻塞探测线程锁；
   writer 占用则等待正常续接，不杀其它原生终端。
3. 锁内重查 authority CAS、目标安全性/inode/hash 和源 hash。仅相同
   字节无需刷新，或严格完整旧字节前缀可刷新；新目标、分叉、变动、
   不完整、在途或无法核验的历史仍保留冲突。
4. 备份与 journal 落盘后原子发布完整源，核验 hash 并保存确定回执。
   释放锁后才进入原任务正常的同 thread ID 续接；回复丢失只核对原
   repair identity/journal/hash，不换 ID 或重做 native turn。

Mesh scope lease 只证明受管任务的 authority fence，不证明其它原生
runtime 或脱网节点静止。`thread/loaded/list`、`thread/read` 的状态属于
所连 runtime，`unsubscribe` 也不保证立即卸载；PID 和文件锁本身不核验
unknown 效果。原生锁各自只覆盖一个 home，跨节点仍须效果对账。
首片需实测跨进程锁互斥、旧前缀完整备份、并发 CAS/文件变化拒绝以及
发布后丢回执的原身份核对；Windows/其它版本留平台适配，不冒称通用。
这一管控仅保护 Mesh 的历史运输，不拦截任意原生 Shell、联网或文件功能。
