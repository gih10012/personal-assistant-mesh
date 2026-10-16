# 独立连续 ClawBot 沟通入口

合同：`owner-entrance-20261016-duplex-v1`，原文 SHA-256
`931ea1ee0f51216092766604d6e91c4f5c3d648c6facab92f5572532bb890401`。
本叶是可选源码/部署入口，不是已部署或本人微信验收，也不完成整个 Mesh goal。

## 实际改变

`entrance_store_class` 叠加在原 Store，不另建通信库/接收器/LLM harness。
只有新入站普通文字、`/status`、`/plan`、`/goal` 和附件先进独立入口 scope。
它们不会自动成为 Leader 任务或产生 ack/media 模板；用户 `/goal` 的文字也
不会被入口运行器自动解释成给自己新建长期 goal。入口模型自行理解意图，
决定自然回复、静默、基本操作或明确交给 Leader。

原控制命令 `/answer`、`/approve`、`/deny`、`/steer`、`/pause`、`/resume`
和精确原生刷新标记继续走原 Store 的交互/租约/unknown 规则。自动控制回执也
先进沟通入口判断，不绕过原审批；模型说“同意”不等于原生权限已授予。

`EntranceWorker` 复用原 Worker 的 `run_once`、原生 Codex app-server、
`native_sessions` 和完整 rollout/压缩。每个入口事件有原持久 task，但它们
共享一个独立 scope/原生 thread；不是每条消息 `thread/start`，也不复制
`leader:owner` 会话或替换原生记忆。入口 peer 不能带 `leader`/普通 `agent`
能力去抢旧任务。旧 Leader 的 needs_review 不阻塞此 scope。

模型经原 `mesh` 动态工具的开放 action 字段调用，无需改原工具 schema：

```json
{"action":"entrance","arguments":{"action":"history","arguments":{}}}
```

- `history {source:events|inbox|outbox,after,limit}`：原归档双方完整分页，当前
  UTC、Asia/Shanghai、最新入站/未处理事件、上次回复；不会消费 iLink 游标。
- `reply {text,observed_latest_message_id,reply_key?}`：显式选定回复；最新消息
  已变化先拒绝，让模型重新核上下文。默认 primary 回复的原发送 ID 固定。
  既有回复 unknown/submitting 不能另换 key/ID补发。
- `silent {reason}`：私有决定，不向本人发送模板。
- `handoff {input}`：在同一事务中用**原 inbox ID**建立原 Leader scope 任务，
  保存来源与入口 task 关联，回读真实 accepted/pending/node/epoch。不把排队
  称为 Leader 已执行；重复原内容只查原任务，改内容冲突，不重投旧输入。
- `status {event_id}`：原事件决定、入口 task、原 handoff、原发送状态。
- `media {attachment_id}`：只可读取已在原授权入站/历史中暂存的不透明引用；
  下载在 SQLite 写事务之外。私有完整文件交原生 agent 实际打开/解析；
  下载/SHA/文件名不是理解，`understanding_verified` 不伪造为 true。

详情见 [附件桥](ENTRANCE-MEDIA.md)。平台 message ID 无损字符串、原毫秒值/
单位/UTC、received/processed/sent、方向、会话、reply/request/task 均分开。
`inbox`/`outbox` 指全部**本地已授权归档**，不是声称平台云端完整历史；入口
仍有原生 `wechat-personal native messages` 读取已同步双方历史的权限。
无须重新开账号菜单，GIH ggiy 对应已确认主账号，只有实际切换/冲突才重查。

当前原 iLink sender 回执不提供平台实际发送时刻，所以 `sent_at=null`，另记
真实提交前/完成观测和返回的平台消息 ID；不能用排队或接收时间冒充发送。
本地微信收件端若有实际时间，可按原授权工具核对，不能据 accepted 声称送达。
`reply_to` 是本地可追溯关联，不伪称已发送平台“引用消息”功能。

## 通知与失败边界

新自动任务结果、review 和控制模板不直接进入微信 outbox，而成为入口事件。
模型先看当前时间/消息/任务，再决定静默或合并有用更新；入口自身的 native
final 只留私有 task，不递归变成另一轮通知，也不自动发微信。
既有 outbox/unknown 行不改、不导入重放、不自动静默删除。

Leader 原 `enqueue`、显式 notify、原生 Claw 权限保留。原跨节点
`RemoteNotifications` 的既有 wire 也保留；本叶不悄悄破坏其 outbox 回执合同。
推荐的新程序化入口可沿当前 worker 的原身份调用：

```text
python3 -m assistant_mesh.entrance_runtime --config /PRIVATE/worker.json action \
  --task-id ORIGINAL_TASK --epoch ORIGINAL_EPOCH --call-id STABLE_ORIGINAL_ID \
  --action notice --payload-file /PRIVATE/notice.json
```

JSON 仅 `{ "text": "明确选择的当前进展或问题" }`，同原 task 租约受核；
`queued_for_entrance` 不是已发/已收到。推荐通道不拦截任何原生 Shell、文件、
网络、MCP 或直接通讯工具。

入口遇到账号前置失败，没有开始原生 turn 的新消息仍可独立归档/处理；
不能因旧 Leader 卡住就拦新入口。若入口自己已经开始 turn 后失败：

1. 原 matching `turn/completed` 必须明确 failed/interrupted，且没有 native goal。
   超时、断连、未知 turn/start 或缺通知不产生继续许可。
2. 只跟踪本实例真正 reader 正常 EOF 与 owned app-server 已 reap；没有自创
   flock/PID absence 探测，更不把这些叫外部 Shell/发送效果闭合。
3. 读取并保存**完整原失败 JSONL**，核同 thread、每行完整、原前缀/全字节
   SHA/稳定文件。通过原 session upload/commit 得到实际 saved/artifact_saved/
   scope ACK 后才记录 worker-reported closed-failure receipt。
4. 原 task 的 side_effect_started/needs_review/operation unknown 保留，原输入
   永不重跑。只允许另一条 epoch0 新入口事件在同 node/auth-home/thread 接续。
   新 resume 在 turn/start 前还核完整失败历史原前缀，不能拿旧 artifact 覆盖后缀。

这不是通用旧 Leader 恢复工程、独立外部效果 attestation 或任意断网 HA。
缺一项证据仍保留新收件/原时间和可读 status，不自动新建 thread/换模型绕过。
此时需用已有直接 Claw/其他入口如实沟通等待或由 owner 补审；不制造一条
“已恢复”模板。native 进程被强杀、未知后台工具继续运行等不在自动闭合范围内。

## Root 单线最小部署（本叶没有执行）

这是 legacy3198 的一次专门适配，不是所有节点必须 Python/systemd/Linux。
native 三模块 SHA 启动 pin 只用于保证本次 adapter 推理适用，未来升级应按
真实新版本适配，不把旧版本设为 Mesh 永久上限。

1. 使用已经审查的 **40-module 原 receiver source**，不是运行数据目录或旧的
   未修复 communication candidate。构建 NEW 目录：

   ```text
   python3 deploy/build-entrance-candidate.py \
     --base-root /PRIVATE/verified-original-receiver-source \
     --destination /PRIVATE/new-entrance-candidate
   ```

   仍走原精确 builder：38/40 原模块字节不变，Store 只抽取 `_ingest_in_db`、
   server 只保留已审 HTTP budget backport。额外四个入口模块；**原 Worker、
   Codex、sessions 不替换**。保留 incumbent routing/owner overlay 对比；实际
   运行树若不匹配不要降级或整库覆盖，先做同一窄 patch 的新 source 适配。

2. 私有 receiver 配置保留原 database/account/port/peers/预算绝对路径和通知
   route，只新增 owner 可信 `entrance` 三字段（同下面 worker example）：
   `owner_ref`、`conversation_id`、`agent_id`。Root 按既有认证方法给独立入口
   worker 一个 `role=worker,node=owner-claw-entrance,capabilities=[mesh.entrance:owner-claw]`
   peer；不是给它 operator bearer、付费 API key 或另一个 iLink account。

3. 从 [worker example](../deploy/entrance-worker.json.example) 填真实 owner 配置，
   入口 workspace 独立，安装完整同 hash 的角色合同到其 `AGENTS.md`。显式
   指定已授权 @163 的绝对 `auth_home` 和 `strict_auth_home=true`，不继承当前
   可能坏的 secondary/default home。模型仅按本次实际 catalog 选择：Root
   2026-10-16 的 metadata probe 有 Luna/default medium、Plus 可用候选；不是
   已经成功推理、永久模型 whitelist 或付费 fallback 授权。

4. 配置媒体 `entrance_media` 的 owner0700 root、现有 skill 两文件 SHA 与固定
   SDK Python launcher，见媒体文档。host3.6可懒启动已有现代 SDK 层；无
   配置明确 unavailable，不自动登录/安装/重 poll。原 raw 媒体 key 只留私有。
   Root 已选择的 VPS helper 安装目录是
   `/home/ai_agent/.local/share/personal-assistant-mesh-capabilities/entrance-media-helper-v1`；
   `media_helper_file` 为该目录 `ilink_media.py`，`python` 为该目录固定
   `python` launcher。这两个实际路径只用于本次部署，不作为其他设备假设。
   当前两源 SHA 是媒体文档的 `31ab57…` / `e34968…`，image/launcher还需
   Root 核实际安装/字节与运行，文档不是部署完成证明。

5. Root 选原 Leader **实际 idle/no owned native runtime** 的受控窗口，保留一致
   ledger/source/unit/account 备份和完整原 native bytes。用既有 owner IPC 原
   PID/instance 只关闭唯一 carrier，核 poll/send callback 全归还；再切换同一
   receiver/API 的 source/入口，避免切断进行中的 Leader lease/RPC。旧 Worker
   不替换、不重启、不恢复 DB/history；旧 unknown/发送身份均保留。不能在
   incumbent 仍可 poll/send 时另开接收器或把本地 stop 当跨分区 grant。
   先核旧 outbox 原ID状态：安装不删除/重投旧消息，也不把已有 pending 自动
   改送入口。若有旧 pending，先按其原记录审查/完成既有授权流程，不能为
   本次安装默许把陈旧模板发给本人；unknown/submitting 不触发重发。

6. 唯一 receiver 的新启动 argv（继续原 owner socket/RuntimeDirectory）：

   ```text
   python3 -m assistant_mesh.entrance_runtime --config /PRIVATE/original-server.json \
     serve --socket /PRIVATE/owned-runtime/control.sock
   ```

   构造强制 `recover_inflight=False`，保留既有 outbox/steering。同原通道启动，
   owner status/stop wire 不变；本模块不自动做任何停服务/迁移/备份/复制 token。
   再以独立 service 启动入口 worker：

   ```text
   python3 -m assistant_mesh.entrance_runtime --config /PRIVATE/entrance-worker.json worker
   ```

   工作目录固定 NEW frozen source。不要让原 Leader service 改用此 worker。
   重启/失败停止沿原 managed task规则，CLI不自动创建新task/thread/新goal。

7. 由本人微信发新消息/图片作真实验收：原 ID/准确时间入库、实际 native turn
   **读取角色 AGENTS**、自然回复/静默、同原 thread 的下一条消息、实际打开
   下载文件、明确 handoff 的原 Leader 回查。Root 看真实 tool/history 后记角色
   read ACK；文档/配置 hash 相等或模型自报不是“已读”。accepted 仍不等于本人
   收件确认。仅这些关键验收后发布进展，不重跑无关全量套件。

State 仍在本次原 authority 的故障域；独立 agent/scope不等于通信 authority
已经迁离 VPS。安全接管、ChatGPT 内插件/官方 Cloud真实入网仍按完整 goal
分别推进。[公网双向插件](REMOTE-CONVERSATION-PLUGIN.md) 提供独立 facade；
本叶不复制它的 journal，也不改 OAuth/MCP handler。以后 owner 私有桥接可
按其 receive/publish API 接入同入口，但不能据 source 说已接通真实 Chat。

## 本叶验证

32 项聚焦测试（真实原3198 Store/SQLite、当前 Store、共享媒体、原API实际
loopback、实际 owned 子进程 reap 与原 reader EOF、完整失败历史上传/下载）
通过，含40项既有近邻的72项组1.064s/0skip；native failed 通知是明确合成
fixture，**0模型、0iLink**。
初轮 fixture 未维持原lease/heartbeat、冻结接线首轮因3198 session commit
无 `ok` 字段拒绝许可，原失败保留；修真实 ACK兼容，不以调用无异常作成功。
四新增运行模块按Python3.6语法检查，未声称实际VPS/手机/模型验收。

官方原生续接依据：[Codex App Server lifecycle](https://learn.chatgpt.com/docs/app-server#lifecycle-overview)：
原 thread/resume 续同一会话，turn/start 产生新输入，真实 turn/completed 才给出
终态。该文档不是本叶任何实际账户、推理或运行结果的证据。
