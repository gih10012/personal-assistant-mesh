# 新 Chat 的原任务自然 final

本叶负责人 `/root/chat_discovery_review_20261019`；分支
`work/conversation-emit-20261019`，worktree 同名，原 base `cd390cc442b3b1ff6b413f1030b2f20678d4fa20`，
续 `82017cf`，不重建分支或合入根协调文档。已核读 main `153d51b` 的
AGENTS、批准阶段、角色 `owner-entrance-20261020-followup-events-v2` 及
Goal/Plan/Tasks 最新有效段。Root 独占生产/SSH/账号/GUI/角色实际分发。
本叶没有执行这些操作，也没有模型请求、凭据或新 App/插件创建。

2026-10-22 有限执行节点扩展：负责人
`/root/final_executor_membership_20261022`，独立分支/worktree
`work/final-executor-membership-20261022` / `final-executor-membership-20261022`，
base `67d20c9f44e3a981a88c1e3471738c828f02ec40`，合并依赖为该 base
既有 final/emit/ready 源码；不改 Root 的 Goal/Plan/Tasks 或生产状态。

## 保证与明确不做的事

已直接核原 git 对象 `3198c877234a8fd0c6912fdbe218f47185effedc`：
Worker `run_once` 在 `native_finish` 和原 session 保存后，已发精确
`POST /v1/task/update`，包含 terminal status、`checkpoint.answer`、
`result`。因此 Worker/native tools/原 thread 完全不换。

仅在受信 ready hook 明确开启后，原 Ingress **新建**且匹配固定
Chat source/subject 的请求，会在同一入站事务保存 reply policy。
旧请求的 submit 重查、status、receive 或已完成历史都不补 admission。
策略冻结准许执行的 worker node；request/task/owner/source/subject/project
从原 ingress 记录派生，模型/worker payload 不能选择收件者或自报批准。

原 `{source, subject, node}` 保持原单节点存储字节、policy digest 和回执
格式。可信 owner-code 也可改用互斥的 `{source, subject, nodes:[...]}`，
明确列出 1–20 个既有入网节点；不接受重复、`*`、空集合或同时给
`node/nodes`。顺序不影响新策略，入库前排序。集合以带版本、不能与合法
旧 node 碰撞的控制字符前缀保存在原 `policy.node TEXT`；不迁移 schema。
ready 逐一检查原认证 peer 的 `role=worker`、精确 node 和 list 类型的
有限 final capability；不会补 grant、创建成员或要求离线候选此刻在线。

每个请求 admission 固定自己的集合。以后扩大 owner 配置只影响新请求；
原单节点和在途集合保持原字节，重查 submit/status 不回填。旧集合必须
仍完全被当前配置允许，否则 fail closed；先完成原任务/交接，再撤去
原成员，不能改旧 policy 或放宽旧任务来恢复回复。成员资格不转移 claim，
实际完成还须是原 authority 上的真实 task.node/epoch/Leader lease。
此扩展不建立跨 authority 共识，不改变 source/subject/project 收件边界。

`legacy-store-conversation-final.patch` 在已审 extraction-patched Store
加 class marker 两行和可选事务 hook 三行；原 lease/Leader fencing
**之后**、原任务 UPDATE/COMMIT **之前**调用。默认无 hook 时仍走原路。
新 facade 对精确 `/v1/task/update` 暂存当前线程已认证 peer；只有
`role=worker`、admission 准许的真实 node、有限 `owner.conversation.final` grant、原
task/epoch/live lease 都匹配才允许该策略任务的 terminal final。
operator 文件、普通 ingress 和 caller 自报字段均不能替代这个授权。

final 文本只取**本次受认证完成请求**的 `checkpoint.answer`，缺省才
取本次 `result` 字符串；不读取保存的 checkpoint/result/native history/
tool stdout。沿8192 UTF-8字节会话上限，长答复有明确截断标记，不默默
以任务状态 JSON 顶替自然答复。空/非文本答复拒绝完成该策略任务。
`completed/failed/needs_review` 会保存 final；waiting/continuing 不伪称完成。

原 completion、`conversation_emits` 消息和 `conversation_task_finals`
回执共享同一个 SQLite 事务。入库失败一起 rollback，不出现“completed
却未保存答复”的成功记录。真实 Worker 原 side-effect/unknown guard
保留；若事务本身未成功，应按原 case 审查，不授权重跑原业务。
**已提交**后侧车/网络/客户端发布失败，只重投原 durable 消息；绝不
重领任务、再调用模型或把完成 POST 换 ID 重送。

固定自动 call ID `automatic-task-final-v1`；固定 source message ID
`mesh-auto-final-<digest>`；原 operation digest 继续绑定 authority/owner/
node/task/epoch/call，客户端 ID `mesh-emit-<operation>`。这些自动身份
不能通过普通 emit route 抢占。同原 final receipt 关闭租约后仍可读。
`/v1/conversation/final/receipt` 只接受原 worker grant 与 `task_id,epoch`；
查询和 operation digest 都绑定**实际完成节点**，集合中其它节点不能
取得该节点的 final 回执；不是用任意成员代替原 producer。
未找到只说 unconfirmed/retry_authorized=false，不授权重发/换 generation。

agent final 与 human-reviewed result 分开；`IngressResults` 表、operator
publication/read gate 完全不改。provenance 明确 authenticated_task_agent /
human_reviewed=false；execution_verified 仍保守，不把 `live=false`
当 Leader 离线。旧23:54诊断只由 Root 单独审原答复后发布，不自动导出。

## Root 最短 source → 私有接线 → 回退

1. 对当前唯一 receiver 的**源码副本**运行 builder inspect；不向 builder
   传 state/DB/配置目录。Root 报告的50 package模块、文件名不带prefix的
   聚合 hash `c2dc96ab64dfa78256f409ab7f4e5a6aae35b1304d8a299e0fed1f3de4889b77`
   **不是**此 builder 所需的全base manifest，不能拿来传构建参数。
   聚合规则为 `sha256(data) + '  ' + 相对文件名 + '\n'` 排序后再 SHA256，
   与既有 Cloud source builder 相同，含原顶层 `.py` launch hooks；
   manifest 与单个 report 文件 hash 也不是同一个值。先检查实际副本，
   只将 inspect 输出的 `base_manifest_sha256` 原值传给下一步：

   ```bash
   python3 -B deploy/build-conversation-final-candidate.py --base-root /PRIVATE/READONLY_RECEIVER_SOURCE
   python3 -B deploy/build-conversation-final-candidate.py \
     --base-root /PRIVATE/READONLY_RECEIVER_SOURCE --destination /PRIVATE/NEW_FINAL_CANDIDATE \
     --base-manifest-sha256 ACTUAL_INSPECT_FULL_BASE_MANIFEST_SHA256
   ```

   输入 Store 必须为 `081633994459ffd2fb253e5514ea398d011b0eb8adcf059b251bbde10a705f08`；
   输出只做精确五行 hook patch，为
   `be2df48e03021f48c382efc4690fa7a8ee5e67da8bd93497e725566532e30c0b`。
   builder 同步改候选 `legacy_fenced_receiver.py` **唯一 Store pin**，
   不放宽为任意 hash。server/worker/codex/sessions/channel 五核心 pins
   字节保持，原入口/Claw/passive constructor/config/launch `.py` 保持。
   `.json` 等非源码不复制；新绝对目标已存在、base/hash/closure 不匹配
   都拒绝。不将现代 Store/Worker 复制进去，也不启动 candidate。

2. 在原宿主做必要无 DB/SDK 导入及准确 source hash 核验；现代 Python
   的3.6 grammar 检查不是宿主3.6.8实跑证据。随Work私有feed集成后固定十库是：
   `conversation_final, conversation_emit, conversation, conversation_runtime,
   conversation_authority, conversation_sync, ingress, ingress_backend, mcp_http_auth,
   conversation_event_feed`。
   特别 `conversation -> mcp_http_auth` 不得漏。`LocalIngressBackend` /
   `RemoteIngressBackend` 从现代 MCP 模块原样窄提至纯 `ingress_backend`；
   `mcp_http` reexport 保持旧调用 API，runtime 直接依赖纯库。冻结
   receiver 不复制/导入现代 `mcp_http` 或其 Events/SDK Extension。
   静态边界保留原 `cloud_jobs,config,entrance_runtime,
   native_messaging,store,worker`，它们及其他既有库都不从现代主仓覆盖。
   builder report 逐文件 hashes、import_graph、changed_original_files 可复核。
   receiver 只 import 纯库，绝不在3.6调用现代 SDK `create_server`。独立
   Events 合并只需保留 `mcp_http` 的 backend reexport，其变动不改变
   此冻结纯库闭包/pins。

3. Root 保留原 ready/IPC/Claw hooks，配置只给明确选定的既有 Leader worker peers 加有限
   `owner.conversation.final`（需要显式进度 CLI 时另给 emit grant）；
   不给 operator bearer，不改 frozen Worker 的 heartbeat/claim 能力。
   在原 `serve_entrance(..., ready=...)` 既有 ready 最后接以下可信代码，
   subject/node 均来自 Root 已核私有配置，不从模型参数填写：

   ```python
   from assistant_mesh import conversation_authority, conversation_emit, conversation_final
   conversation_authority.attach(api, sources=original_enabled_sources)
   conversation_emit.attach(api, enabled=True, sources=original_enabled_sources)
   finals = conversation_final.attach(api, enabled=True, policies=(
       {'source': 'chatgpt-chat', 'subject': fixed_chat_subject, 'node': original_leader_node},))
   ```

   若 Leader 可在已核的两个原节点间接替，同一位置可显式改为
   `{'source': 'chatgpt-chat', 'subject': fixed_chat_subject,
   'nodes': [existing_cloud_node, existing_laptop_node]}`。复用
   `deploy/conversation-final-ready-fragment.py.example` 的逐节点 grant
   校验；两个变量必须来自已有认证 worker 配置。旧 admission 仍是原
   单节点，不能把重启后更宽的配置写回旧行。不固定 VPS，也不以
   `nodes` 绕过原 task/epoch/Leader lease。

   sources 与原 installed authority 保持，Cloud 未接通不自动加 grant。
   保留 `serve_entrance` 的 `recover_inflight=False` 被动构造；不新建第二
   Store/receiver/队列或常驻 loop。Root 独占正常备份/唯一receiver受控
   切换；五核心/原 Worker/native thread 不为 final 重启或重排。

4. Chat 原 OAuth factory 的**同一个** `ConversationService/Bridge` 显式
   `conversation_emit.enable_projection(bridge, enabled=True)`；正式 Cloud
   已有同 source bridge 时同样配置，未启用 policy 的 Cloud 仍只显式 emit。
   不新建 OAuth/App/端点，不改八工具/路由/header/auth/no-retry；所有
   `/v1/conversation/*` 与 read route 仍私有，不加到公网 nginx。
   receive 只投影已保存消息；不回填 task result、不 drain 客户回复。
   source created_at → original_time，接收/投影时间各自保存；投影在
   可见前保存原 publication epoch，重复核对。同 task epoch2 后回复
   epoch1 旧消息得到 `stale_original_task_epoch`，不入新 steering。

5. 实际验收一项**新、明确批准**的 Chat 请求：原 Leader 原 thread
   自动完成，无额外 final CLI；原 completed 与 deterministic final
   receipt、同 conversation 自然文本都读回。丢 ACK/投影恢复只查原
   receipt/游标；不为了检查再提交业务。源消息持久、平台 tool 消费、
   模型读懂和本人验收分别记录，不把任一2xx/状态布尔当全闭环。

回退：Root 受控恢复旧源码与原 ready，关闭 final attach/侧车 projection，
保留新增表与全部原 receipt，禁止删库/回填/重排。旧读取、approved
publication 和 Claw 路由保持。已启用 policy 的未终任务应先按原ID安排
完成/交接；不能在进行中静默撤 hook 后声称仍保证自动 final。

## Events 的可选弱耦合与检查证据

`ConversationFinals.completion_events(after=0, limit=20)` 仅供可信 owner
代码读取提交后的 durable refs，无 HTTP/tool publisher、网络或后台 loop。
event_id 绑定原 operation/status；message_id 就是客户端稳定消息 ID。
原 ingress project_id 缺省 None，不能猜项目或扩租户授权。`needs_review`
映射 `leader.attention_required`，已终 `completed/failed` 映射
`task.completed`；具体业务成功与否仍读原 task/final，不靠事件名断言。

独立 Events 叶可在完成事务**之后**用相同 refs 调可信
`assistant_mesh.mcp_events.MCPEvents.record_task_event(**refs)`，收到
持久 outbox 回执后才推进原 feed cursor；丢 ACK 同 event_id 幂等查/恢复，
绝不重跑 task。这个叶无 Events import/merge 依赖，不宣称 Work 已接收或
普通 Chat 可被任意 webhook 唤起。普通 Chat 回查由原聊天原生任务负责。

Root 已独立在本原 worktree 跑 final4+emit3，共7项0.108s通过；作者没有
重复这7项。另作者只跑 builder1项：0.622s/0skip，实际冻结源fixture、
两个精确 patch、所有非允许模块字节保持、helper pin、完整闭包在 `-S`
无SDK且禁用 sqlite.connect 下导入通过。随后窄提取 pure backend，同一
builder检查0.609s/0skip另核旧 import API class identity、receiver不import
mcp_http；两类 AST 与82017cf原实现完全相同，没有重跑原7项。只属本地
现代解释器，不冒称
宿主3.6.8实跑、部署或真实 Chat/Leader 验收。原 Goal 未完成。

2026-10-22 扩展在本机 Python3.14.7 的有限检查：
`tests.test_conversation_final tests.test_entrance_assembly` 共10项通过
（原7项加3项新边界），0.102s/零skip；新项覆盖真实 claim 的节点/epoch
约束、原单节点策略/回执重启保留且在途不扩权、所有集合成员精确 grant
与输入快照。首轮新测试把 submit 重查的动态 status/task_created 与首次
回执整体比较而失败，已改成核原 task ID、未新建和完整旧 policy 行，
没有放松 runtime 检查。
原冻结 builder 单项0.649s/零skip通过，十纯库闭包、原五行 Store hook/
五核心 pins 和无 DB/SDK import 保持。21成员 manifest 全部通过；
仅 final、ready、builder 三个真实变更成员更新 hash。未运行真实Mesh/
模型/GUI/故障演练或平台验收，生产接线仅由Root执行，验收留Leader周队列。
