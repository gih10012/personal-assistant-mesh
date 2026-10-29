# 原任务的本地部分进度复核与接续

此接口是默认不可供 worker/model 调用的维护能力。不会自动把任意 Shell、
工具成功返回或 quota 错误解释为安全；不调用零执行重排脚本，不创建新任务、
thread、Goal 或模型探测。代码实现不等于已部署或已接受任何真实任务。

可信部署侧单独配置一个 peer：`role:"native_maintenance"`、稳定
`reviewer_id`、独立私有 token_file。该凭据不得复用 operator/worker/ingress
凭据或提供给业务模型。相同 token 的角色混用在 API 启动时拒绝。
维护角色只可访问四个窄 POST 入口：

| 路径（均在 `/v1/maintenance/native-partial/` 下） | 行为 |
| --- | --- |
| `inspect` | 原 task/session/artifact 的精确 CAS hashes 与原身份 |
| `history` | ≤64KiB gzip 分块不可变暂存，不改变任务/session |
| `accept` | 验证已复核规范，保存 before/receipt 并原子接受 |
| `receipt` | 原 acceptance_id 查回，不重开旧执行 |

`owner_reviewed_completion` 用于 owner 已核实交付的业务：同 task/epoch/thread
保存复核结果，状态 completed，不启动新 turn、不再消耗模型。结果可从原
`/v1/task/status` 及原调用方发布流程读取。不会自动补发旧 outbox 或更换未知
消息的 operation；实际通知/投递仍按原 transport receipt 对账。这也不伪造
原 native assistant answer 或自动调用 conversation-final hook。

`continue_remaining` 用于仍有剩余业务：原 task 变 pending，accept 不增长
epoch；下一次正常 claim 才增长 epoch，沿原 thread 明确开始后续 turn。
输入包含已复核进度和本人提供的剩余工作，不重新附加整份原输入。原 input、
checkpoint、失败结果、session、旧 artifact 字节及旧 unknown 均在 before 中
保留。原 Goal/accounting/provider 身份保持；模型与 reasoning 继续走部署
profile/实际目录校验，prompt 和此 receipt 不证明实际 model/effort。

首版拒绝 active/paused/blocked Goal 的此路径：其续接应走原 Goal controller。
checkpoint 与 session Goal 必须一致，真实 Codex goal/read 还须在处理恢复的
host 请求前匹配已复核快照。缺少真实 Goal 状态不能当成 Goal 已消失。

## 复核规范

`accept` 的 JSON 顶层字段严格为：`protocol`（
`owner-native-partial-continuation/1`）、`binding`、`expected`、`parts`、
`operation_id`、`closure`、`known_local_progress`、`disposition`、
`instruction`、`result`。测试 fixture 给出可运行的完整合成示例。

- `binding`：`acceptance_id,task_id,expected_epoch,thread_id,turn_id,sha256,size`。
  hash/size 指完整原 native JSONL，包含全部既有 turn、元数据与原始内容。
- `expected`：直接保留 inspect 返回的 `task_sha256,session_sha256,artifact`。
  artifact 包含旧 id、完整压缩字节 hash 和分块数量。接受时 CAS 重核原绑定。
- `parts`：完整 JSONL 用 `gzip.compress(raw,mtime=0)` 后的64KiB分块数。
  暂存不替换 live session；接收时解压核完整 hash/size，要求旧 artifact
  解压字节是新完整历史的原前缀。最多64MiB原文/16MiB压缩包，超预算拒绝，
  不截断、不使用应用摘要冒充原生历史。
- `closure`：原 `node,auth_home,thread_id,turn_id,runner_id,runner_pid,
  runner_birth,runtime_work_sha256`；`kind` 固定
  `owner-observed-native-runtime-closed`；`native_owner_live:false,
  owned_children_closed:true,forced_cleanup:false,goal_controller_active:false`；
  `goal_snapshot_sha256` 为规范化原 Goal（无 Goal 则 JSON null）的 SHA256；
  `evidence_ref,evidence_sha256` 指私有原 runtime close/owned-child 关闭证据。
  runtime_work_sha256 是规范化原 work JSON 的 SHA256。**wrapper heartbeat
  活着不等于 native 还在执行；children 为空也不能单独证明没有外部效果。**
- `known_local_progress`：选定 turn 每个工具调用都要一个复核项，包含
  `tool_call_id,operation_id,call_sha256,output_sha256,effect_kind,
  unresolved_external_effects,resource,evidence_ref,observation`。
  call/output hash 是各自完整 JSONL 行（含换行）的 SHA256；operation 必须
  对应原 task 原 operation。effect_kind 只接受
  `owner-reviewed-read-only` 或 `owner-reviewed-local-progress`，且
  unresolved_external_effects 必须 false。observation 保存原命令回执对应
  的真实 HEAD/clean/已创建路径等私有核对结果。
- `instruction`：continue_remaining 必须是明确剩余工作；`result:null`。
  completion 则 instruction 为空，result 为 owner 核实的完整交付结果文本。

规范化 hash 使用 `native_partial_continuation.digest(value)`：JSON 键排序、
ASCII 转义、无空格、拒绝 NaN。它证明状态绑定与字节完整性，**不证明任意
Shell 安全，也不证明 owner 判断独立于执行方**。receipt 明确标记
`evidence_basis:trusted-owner-reviewed-local-effects-and-runtime-closure`、
`independent_execution_verification:false,effects_zero:false`。

未配对的 tool/output、未完成 native item、未知执行 record、选定 turn 后又
出现另一 turn、仍有 native owner、forced cleanup、未知外部效果、未结算
managed/remote/child/interaction/steering、同资源其它 unknown 或 CAS 变化
均拒绝，仅此任务等待。普通 JSON `completed:true` 不具备解除 guard 的权限。

## 维护侧操作顺序

私有 client config 复用既有 Client 的 `control_url,token_file`（需要时加
原 unix_socket）；这里的 token 只能属于专用维护角色。以下均由 Root 在
部署、检查真实效果之后执行，不是本分支已执行的操作：

```sh
python3 -m scripts.native_partial_continuation --config /private/maintenance-client.json inspect --task-id ORIGINAL_ID
python3 -m scripts.native_partial_continuation --config /private/maintenance-client.json upload --review-file /private/owner-review.json --rollout /private/original-full-rollout.jsonl
python3 -m scripts.native_partial_continuation --config /private/maintenance-client.json accept --review-file /private/owner-review.json --approved-sha256 EXACT_REVIEW_FILE_SHA256
python3 -m scripts.native_partial_continuation --config /private/maintenance-client.json receipt --acceptance-id ORIGINAL_ACCEPTANCE_ID
```

先取 inspect 与完整原历史、逐 op 查清所有效果和关闭证据，再复核规范文件
精确 hash。upload 不改变业务。accept 失去 ACK 时先 receipt 查原 id；同 id
同内容重复接受只读不可变回执，不重置已经推进到新 epoch 的任务。

## 原 runner 的协作退出

`RuntimeStore.retirement_resolver` 默认 None，`retire_closed` 会拒绝。
可信部署代码必须安装 resolver：使用独立维护身份从原 authority 的 receipt
入口读取并核原 acceptance_id；不要从普通 worker/model JSON 接受 receipt。
接受记录内的 closure 还须匹配原 runner/PID/birth/task/epoch/work hash，且
实际 PID 身份存活、直接 children 为空。仅接受原 phase unknown 的绑定任务，
无 task 的 claim unknown 继续保留等待核对。

部署侧可调用 `store.retire_closed_runner(operation_id,runner_id,acceptance_id)`，
或在安装 resolver 后使用本地 supervisor 的 `retire_closed` 控制动作。
它追加不可变 retirement 记录，保留原 work unknown 正文，关闭该 runner
后续 admission。下次可信 heartbeat 返回 stopped，原 worker loop 自行退出。
不发信号、不伪造 checkpoint、不清业务 guard，不把请求退出当进程已死。
观察到实际退出后，supervisor 仍保留 process_exited_work_retained 状态。
重发同 operation 只查原回执，不重复退出动作。

部署 resolver 尚未接好时，可继续使用原 stage → standby → activate 接收
独立新工作；旧 retained/unknown 保留，不必为恢复其它业务先清旧账本。

## 新增存储覆盖

原 global ledger：`native_partial_acceptances`（完整 before task/session、规范与
回执），`native_partial_before_chunks`（旧 artifact 原字节副本），
`native_partial_history_parts`（不可变完整历史暂存）；新 live artifact 仍使用
原 `session_chunks`。三张新表均显式 append-only，不以 prefix 绕过复制覆盖。

节点本地 runtime.sqlite3：`native_closed_runner_retirements`，保存原 work
正文、原 state 与 authority 接受回执；不是原业务 ledger 的替代副本。
Root 必须把这些表显式纳入相应复制/恢复政策，再决定真实接线。
