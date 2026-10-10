# Owner 显式追加工作：同 task 续接合同

`POST /v1/task/continue` 是 operator-only 的**新工作授权**，不是失败重试、
unknown 对账、native history 导入或新 Leader/thread 创建。Worker、viewer、
ingress 和 A2A peer 不能自行调用；没有新增 CLI/MCP 工具入口。此文档和
离线测试不是实际部署或 native turn 完成的证据。

请求严格包含以下五个字段，不接受 `force`、身份/权限覆盖或自造 session：

| 字段 | 合同 |
| --- | --- |
| `continuation_id` | 本次追加工作的稳定 ID，至多 200 UTF-8 字节；丢回执沿用原 ID |
| `task_id` | 原 task ID，至多 200 UTF-8 字节 |
| `expected_epoch` | 原 completed task 的正整数 epoch，不接受布尔值 |
| `task_result_sha256` | 原完整 result 字符串的 UTF-8 SHA-256，64 位小写 hex |
| `instruction` | 本次新增指令，加入固定续接说明后的 task input 至多 65536 字节 |

Authority 在同一个 SQLite writer transaction 内核对原 task 为
`completed`、epoch/hash 未变、未暂停，checkpoint 中原 `side_effect_started`
必须明确为 JSON `false`。缺失、`0` 或字符串 `"false"` 都不等价。原 scope
的其它活 lease/未决效果、原 task 的活子孙任务、原 task 及递归子孙和同 scope
所有 task 的未结算 managed allocation/dispatch 和 remote child mapping，或原 task 未决 steering
均阻止接受。`stopped` 是具有 provider-reported quiescent settlement 的 dispatch
终结态；现有 allocation 在全部 dispatch 结算后变为 `completed`，并不写
`stopped` allocation，也不把 timeout 当作结算。
超期的 accepted/running/unknown 资源 hold 不会因此被释放或当作零效果。
同 scope 的另一 task 即使已 `completed` 且 checkpoint 明确为 `false`，其
unknown/held managed operation 或未终结 remote mapping 仍阻止续接；无关
scope 的独立业务不因此阻止原任务，也不修改其 hold 或回执。
这只核对本 authority 已有关联账本，不冒称跨节点一致性，也不凭完成
状态独立核验任意 native 或外部效果。

原 checkpoint 必须具有 Codex/Pi harness 和对应原 `thread_id`；Codex ID
采用现有 canonical UUID 合同，Pi 保持原生 opaque session ID 及实际
`switch_session(sessionPath)` 所需的原 `pi_session_file`，该私有路径不进回执。
原 scope/harness 的 authority `native_sessions` 必须仍选中同一原 thread，
Pi 的 session file 引用也必须匹配。此检查
只绑定已有 native source，不把 metadata JSON、session row 或无 artifact
的测试 fixture 宣称为完整模型上下文，也不触碰 native rollout/压缩。

接受前把原完整 input/checkpoint/result 及 authority native row 保存到私有
`task_continuations` 不可变 journal，与 content fingerprint 和接受回执一同
提交。原 task 的 input 改为**仅本次新增 instruction**及“不重放旧 turn/
operation”的说明；不再次把旧完整业务 prompt 提交给模型。原 task、scope、
context、required、native checkpoint 与效果标记保持，状态变 `pending`，
node/deadline/result 清为空；epoch 不在这里增加，由正常 claim 推进。
这不主动调用 turn/start，也不保证 native runtime 已空闲或业务已完成。

同 continuation ID、同内容的重试只返回**原不可变接受回执**，即使 task
已进入后续 epoch、起效、完成或又暂停，也不重排队、不清除任何状态。
同 ID 不同内容拒绝；不同 ID 竞争同一个 completed epoch，仅一个可接受。
回执的 `task_state_at_acceptance` 是当时状态，不是当前运行状态。
拒绝不恢复 paused/failed/needs_review/unknown，不新建 effect/task/Leader ID
绕过旧动作，也不向外发送通知。

旧 ingress 成果出版记录及各原 effect ID 继续保留。成果读取仍核对当前
task epoch/result：追加工作使旧成果不能再被误认作当前任务成果；本合同
不新增成果 revision/republication 工作流。若需读旧完整内容，由 owner
访问私有 continuation journal，不自动转发原 native answer。

Mesh 只增加这一受管追加工作入口；原生 Shell、文件、联网和 MCP 不由
它拦截。当前验证范围是隔离账本中的 CAS、权限、幂等和原字节保留，
实际同原线程的新 turn 与完成回执须独立验收，不从测试推导。

2026-10-10，`e2397f1` 两端完整 1267 项测试及公开 CI 通过并加载；当前
配置与原任务/native/历史块/effect 表指纹保留。原 PAM-004b SELECT task
completed epoch 1 的 owner CAS 已实际接受，旧结果 hash 绑定不变；正常
claim 推进同 task 到 epoch 2，原 Codex Leader thread 开始新 turn 并 completed。
旧完整 result 的 UTF-8 SHA 仍与源 CAS 一致，完整私有 journal 已独立读回。
模型选择/原 operation link、受管 GET 产物/结算/容量归还另经实际核验，
详见 [canary](EGRESS-CANARY.md)，不从续接接受直接推导业务成功。
