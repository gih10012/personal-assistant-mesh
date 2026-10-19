# 原 task agent 的自然消息回传

PAM-006 双向会话薄叶；负责人 `/root/chat_discovery_review_20261019`，分支
`work/conversation-emit-20261019`，同名 worktree，base `cd390cc`。
本叶只写库、CLI、隔离检查与本文件。Root 独占 peer grant、ready hook、
实际分发/核读和部署；没有生产、GUI、模型、账号、凭据或新 App 操作。
GOAL/PLAN/TASKS 不改，完整 Goal 未完成。

## 授权与原身份

新增 `conversation_emit.py`，默认不安装 route、不建表、不创建 Store/
Worker/任务或常驻 loop。Root 在**原已打开 API** 的既有 ready hook 后
显式调用：

```python
from assistant_mesh.conversation_emit import attach
attach(api, enabled=True, sources=('chatgpt-chat', 'openai-codex-cloud'))
```

保留原 ready/ConversationAuthority/Claw hooks。Root 只给实际原 Leader
`role=worker,node=原node` peer 增加有限 `owner.conversation.emit` capability；
不交 operator bearer，不凭机器上有 operator 文件推定授权。原 worker
config/token 引用可复用；heartbeat/claim 所需原能力和六核心 pins 不改。

仅新增三个**私有**精确 POST route，继续不进入公网 nginx：

| route | 身份与作用 |
| --- | --- |
| `/v1/conversation/emit` | worker + node + 有限 grant + 真实 live task/node/epoch/Leader generation；写一条消息 |
| `/v1/conversation/emit/receipt` | 同原 worker/node/grant/task/epoch/call ID，只查原回执；lease 结束也不重新写 |
| `/v1/ingress/conversation/messages` | 原 ingress peer 的 owner/source/subject 与 `tasks:read`，只读该原 request |

write payload 只含 `task_id,epoch,call_id,message_id,kind,text`；kind 为
`progress/message/final`，text 沿用8192 UTF-8字节上限。接收方由 authority
在同一原 Store 事务中，从唯一 `ingress_requests` 记录派生。没有该绑定
的内部/Claw任务不可随意选一个 Chat 客户端；payload 不能选择 owner/
source/subject/request/conversation/role 或审批身份。普通自然提问用
`message`；真正 native question 继续使用原 interaction/Authority 链路，
需要等待本人选择时使用已有 native requestUserInput；emit 不创建等待
interaction，也不以普通文字伪造原生 question、批准或执行成功。

原始 call/message ID 和正文指纹不可改变；重复同内容返回旧回执，修改
正文或另换 call ID 重用 message ID 冲突。唯一 INSERT 与回执同事务。
网络失败不证明没写入；原 receipt 未找到只表示 unconfirmed，不授权
重发、换 ID、换 epoch 或换 node。不存在任务创建/续租或原生工具重放。

## Sidecar 的默认关闭投影

在实际 Chat/Cloud 原 factory 中，取得已经配置同 source/subject 的原
`ConversationBridge` 后显式安装投影：

```python
from assistant_mesh.conversation_emit import enable_projection
enable_projection(bridge, enabled=True)
# Cloud factory 内部已创建 bridge 时使用 service.runtime。
```

无此 hook 时原 receive 行为保持；有 hook 时它只读 authority **已经由
agent 写出**的消息，再映射到原 ConversationService。不会从 task 状态
或 raw result 生成自然回复，不调用 emit、不创建任务、不 drain 客户端
回复，也不把 read scope 变成 producer 写权限。原状态 JSON/真实问题/
已批准成果路径保留，但它们不冒充新的自然文字。

每条源消息的 `created_at` 映射到 `original_time`；侧车的 `received_at`
与 `processed_at` 是实际投影时刻，另保存投影时间和源 message ID。
客户端稳定 message ID 为 `mesh-emit-<operation digest>`，避免与客户端/
状态/问题 ID 冲突。producer receipt 已返回该映射，客户端 provenance
保留源 call/message ID、源 epoch/创建时间，且明确
`producer=authenticated_task_agent,human_reviewed=false`。
它不是 provider 来源证明、human approval 或独立 execution 验证。
中断或重建后，按源 cursor 和稳定 ID 只恢复同消息投影，不产生另一条
源写入。消息仍需客户端 receive/wait，关闭客户端的后台唤醒另属待办。

原 `IngressResults` operator-only 发布/read gate 和内容 CAS **不变**。
`final` 是 agent 向原委托客户端选择发送的自然结论，不是 approved
publication，更不自动导出 `task.result`、checkpoint 或完整 native history。

## 原 Leader 的最短 Role/当前 task 用法

Root 将下列使用合同真正分发到原 Leader，并核其完整工具读取回执：
“当前任务有已授权 Chat/Cloud 原 ingress 绑定且 peer 获 grant 时，选择
有用进展、自然提问或结论，用原 worker config 和当前 task/epoch 显式
emit。final 必须在本次 lease 仍 live、任务 finalize **之前**发送；原生
final 文本和 completed 状态不会替你发送。丢 ACK 只查原 receipt；不得
使用 operator 配置代发或把普通回答升级为 human approval。”

原3198 `resource_access.cli.argv` 提供原 config 路径，
`resource_access.cli.routing_task_binding` 提供当前 task_id/epoch。旧原生
thread 无需新 dynamicTools；使用现有 Shell。将正文 JSON 保存到仓库外
本人所有0600文件，仅包含 `message_id,kind,text`；保留原 ID 后调用：

```bash
PYTHONPATH=/PRIVATE/REVIEWED_FROZEN_SOURCE python3 -B -m assistant_mesh.conversation_emit \
  --config /PRIVATE/ORIGINAL_WORKER.json --task-id CURRENT_TASK --epoch CURRENT_EPOCH \
  --call-id STABLE_ORIGINAL_CALL emit --payload-file /PRIVATE/MESSAGE.json
```

丢 ACK 时同 task/epoch/call 改为 `receipt`，**不带 payload-file**；不要重新
emit。CLI 只发一次请求，异常返回 unconfirmed，不读出或输出 token。
过期时新 emit 被拒；不能为了 final 延长 claim、造 keeper 或完成后在
read hook 补写。若要保证 finalize 必发，需要另行明确设计，不在本叶。

Root 部署时更新三个非核心库 `conversation_emit.py/conversation.py/
conversation_runtime.py` 的冻结闭包/pins，并在实际 Leader 可执行的已审
源码路径提供 CLI。不得为解决 import 复制现代 Store/Worker/Codex/
sessions/server/channel。这里没有切换 receiver 或注册新 public route。

## 已有小组与真实余项

3项隔离检查使用真实 SQLite ingress、heartbeat/claim/epoch/到期、原
question/receipt 及 Chat/Cloud source：覆盖默认off、有限 grant、严格参数、
原身份隔离、自然消息/时间/provenance、原批准表不变、重复/冲突、CLI
在真实提交后模拟丢 ACK 并只读 receipt。没有调用 SDK/模型/外部服务。
Python3.6 grammar 只是现代解释器 parse，不等于宿主3.6.8实际通过。

Root 尚须独审冻结闭包、实际 finite peer grant 与两侧 hook，正常授权后
让原 Leader 在同原 thread/task 真实 emit 一条进展/自然结论，并由对应
Chat/Cloud 的同 conversation 读回。source commit、持久 receipt、客户端
tool消费与本人验收分别记录，不能把 fixture 或状态 JSON 当闭环验收。
