# 手机接线前只读交接（2026-10-31）

本片仅查看实际服务、精确 TLS 配置和既有源码，不改接收器、账号、签名、
私有配置或业务状态。OAuth bundle 原 WIP 留在其独立分支，未继续扩展。
原商业 unsigned HAP、API26/arm64 构建、调试证书与 CSR 配对证据继续复用，
没有重新构建、检查 Profile/密钥、读取浏览器材料或调用模型。

## 本轮实际确认

- VPS 用户 `assistant-mesh.service` active/running，入口仍为已部署的
  `amita-native-partial-receiver-20261030.1`。私有 API 17680 与 phone 17684
  由同一个接收器进程监听，均为 loopback；没有另建 Store/receiver。
- `assistant-mesh-cloud-node.service`、`amita-cloud-supervisor.service` 和
  系统 `personal-assistant-mesh-ip-tls.service` 均 active/running。
- 实际 Nginx phone allowlist 只有 `/enrollment/node`、`/phone/node`、
  `/v1/harmony/updates/manifest` 和精确 64 位小写 hash artifact 路径，均转到
  127.0.0.1:17684，保留 Host、固定 HTTPS、关闭上游自动重试。owner 管理接口
  未进入公网 allowlist；其余路径仍 404。
- 实际旧 ready 链构造带 public trust 的 UpdateService，再挂同一原 API 的
  phone listener。私有 data-release 下 manifest.json 存在，1304 bytes；
  本轮未读取其正文、服务 token 或签名密钥，也未把“文件存在”冒充下载/签名验收。
- 电脑 `amita-hdc.service` enabled，实际 listener 仅 loopback 8710。未执行
  HDC 设备动作、开放校园无线调试、操纵桌面或改 USB 状态。
- 本轮所查 phone-v1 目录中没有 owner binding 目录。没有创建 node、binding、
  enrollment、challenge、nonce 或任何假手机结果。

2026-11-01 Root 已回传：正常 Edge 完成真实设备登记及 Profile，商业 HAP 已用
原密钥成功签名，尚未安装。本片未读取 Profile/密钥或重做构建；交付安装沿
Root 的签名/安装清单执行。以上仍不表示真手机已经入网或后台回调已发生。

## configuration-only plan 的精确状态

源码确认，无 enrollment ID 的 `phone_owner ... plan` 只发送
`POST /v1/phone/owner/plan {}`，返回 configuration_only、challenge_created=false
及已加载身份/operation 观察，不初始化任何 challenge。现有 10 月 29 日
phone-owner-live 已真实通过的证据仍保留。

本轮准备使用当前 receiver namespace 和已存在的 cloud operator 配置再调用
一次，但 Click 在执行前拒绝 SSH/env/module argv：
`check 1 is neither read-only nor a recognized check`。故本轮没有发送这次
plan 请求，没有不确定响应需要重试，也未绕过或新建验证框架。已把确切 CLI
路径参数交 Root，后续可沿原已支持的私有 owner CLI 验证入口完成一次；
不要把这次工具分类拒绝记录成服务调用失败或新鲜 plan 成功。

## 三种身份与任务不能混用

Huawei UDID 用于设备 Profile。应用 node_id 则由 NativeServices 首次
initialize 在应用私有状态中生成 `harmony-UUID`，后续读原 snapshot 保留。
不能提前用 UDID、硬件序列号或随机 owner 占位代替应用首次启动的真实 ID。

LiveTaskGate 在原 Store 事务内验证实际绑定的 task/epoch，要求原 task 仍
running、原 node/租约有效，并由 Store 执行相应 Leader 代际校验。随后必须
匹配原 ingress request_id；没有 ingress 行时才按原 task context 中的
request_id/origin message_id 观察。owner 操作还要求 session_scope 精确为
`leader:project:amita-harmony-onboarding`。

因此，已经 completed 的 phone-owner-tools **开发任务**不能再次当作真实入网
任务；正在进行的 S2 task/epoch 也不属于这个 scope。保留它们的完成结果、
原 unknown/before、原 thread 和全部收据，不恢复或改写来通过入网 gate。

2026-11-01 已在现有电脑 passive DB 用 `mode=ro`、`query_only=ON` 和只读事务
查询精确 scope 的 task/request/epoch/status/node/deadline 元数据，不读取 task
输入、评教或其它 scope 的业务内容。该观察点 cloud generation=2，
cursor=head=1038638；只发现一条已完成的原工具开发任务：epoch=1、completed、
node/deadline 均为空、未删除。其原 ID/request 已私下回传 Root，本文不记录
真实业务标识。另查 task.scope 等于目标、但 context.session_scope 不匹配的
记录为空。因此这个观察点没有可复用的真实 live 入网任务；不能复用开发任务。

## 已有创建/领取入口：不需新增准入代码

普通 CLI `submit` 仅发送 input，但实际 operator `POST /v1/tasks` 支持完整
id/input/required/parent_id/context，并调用原 Store.create_task。捕获的实际
server.py:168–169 与 phone 部署加载链 pin 一致，SHA256 为
`cc5bb8b492d7e03f68a5f8720eb1399d9ab4ac8e303f46919713c0a6592fd19c`；
不是拿现代 CLI 的字段限制推断旧服务不支持 context。原 scope 专用 profile
也已由原 worker 装配保留。无需新接口、重复 Store 或重开已完成任务。

Root 后续真正开始入网业务时，在 Git 外 owned-0600 私有 payload 文件保存
一次完整请求；下列均为待替换模板，未创建任何新 ID/任务：

```json
{
  "id": "ROOT_RETAINED_REAL_ONBOARDING_TASK_ID",
  "input": "执行本人真实鸿蒙手机首次入网及后续独立能力往返；当前等待真实应用首启 node_id，不提前创建挑战或假身份，沿原 ID 对账。",
  "required": ["agent", "mesh.node:ACTUAL_AUTHORIZED_COORDINATOR_NODE"],
  "context": {
    "project_id": "amita-harmony-onboarding",
    "agent_id": "amita-harmony-onboarding",
    "session_scope": "leader:project:amita-harmony-onboarding",
    "request_id": "ROOT_RETAINED_REAL_ONBOARDING_REQUEST_ID",
    "source": "owner-real-phone-onboarding"
  }
}
```

task ID/request ID 由 Root 为这次实际业务请求预先保存，不取旧开发任务 ID；
node routing requirement 来自已授权 worker 的实际 node_id/capabilities，必须
是现有授权集合的子集，不能编造节点、扩大权限或冒用手机 node_id。若原真实
请求由入口创建，优先沿其原 task/request 继续，不再另建。

以下命令只供 Root 在可信已部署 namespace 中后续执行，本片没有执行：

```sh
python3 -B - /PRIVATE/existing-operator.json /PRIVATE/real-onboarding-task.json <<'PY'
import json, sys
from assistant_mesh.config import private_json
from assistant_mesh.worker import Client
client = Client(private_json(sys.argv[1]))
body = private_json(sys.argv[2])
assert body['context']['session_scope'] == 'leader:project:amita-harmony-onboarding'
assert body['context']['request_id'] and body['id']
created = client.request('/v1/tasks', body)
assert created['id'] == body['id']
state = client.request('/v1/task/status', {'id': created['id']})
print(json.dumps({key: state.get(key) for key in ('id','scope','status','node','epoch','deadline')}))
PY
```

丢创建 ACK 只查原 task，或按原 immutable id/body 查回；不换新 ID。此 direct
operator 路径不增加 ingress 行，`settings.entry_request_id` 应取真实的
context.request_id；若该 task 已有原 ingress 行，则以其原 request_id 为准。

让 required 匹配的原已授权 worker **正常领取**，再用上面的单 task status
查回真实 running/node/epoch/deadline。原 Worker 的 `/v1/claim {}` 是一般队列
领取，不接受按 task_id 过滤；不能往 body 编一个 task_id 假装定向领取，也不
另起同凭据消费者抢领。若该原 worker 正在做其它工作，Root 协调其真实安全
边界/现有可用 worker；任务可先 pending，不能伪造 running。

原 claim 给 90 秒任务租约；原 worker tick 持续发送 `/v1/heartbeat` 和
`/v1/task/update {id:实际任务,epoch:实际领取epoch}` 维持活跃，省略 terminal
status 和 checkpoint，不制造 safe/native_close。若 required 含 leader，还须
原 Leader 任期/租约有效。只发 node heartbeat 不会续 task deadline。真正正在
承担此任务的已认证 worker，其原续租消息形状为：

```python
worker.request('/v1/heartbeat', {'capabilities': existing_worker_config['capabilities']})
worker.request('/v1/task/update', {'id': claimed_task['id'], 'epoch': claimed_task['epoch']})
```

这不是额外保活脚本或另一个假 worker；优先复用原 tick。状态不是 running、
租约丢失、epoch/Leader 变化时停在等待，重新从原 authority 取得有效状态再
绑定，不沿旧 epoch 准备挑战。人工独占“按 ID 原子领取”的 owner 命令当前没有，
但正常 worker 领取足以完成本方案，不能据此声称现有任务创建/入网存在代码
缺口。现在停在等待真实 app node_id；仍不创建 binding/challenge/nonce。

## 最短接线顺序

1. Root 按自己已有的签名/安装清单交付已签 HAP，不重复 Profile/构建步骤。
2. 合法签名安装并首次打开应用，取得并保留应用真实 node_id。此时尚未登记。
3. Root 确认真实 enrollment task 的 live node/epoch/request/scope，按
   [owner 操作契约](PHONE-OWNER-OPERATIONS.md) 放置固定 binding index、settings
   和原 Bootstrap 文件。ID 到达以前仅保留模板，不创建真实 challenge。
4. Root 用同一 enrollment ID 执行 `prepare-existing-binding`，再经获准初始
   设置通道交付 relay-export。手机主动调用原 `/enrollment/node` 并产生完整
   真实字节结果。丢 ACK 只查回原 operation，不换 ID/nonce 重发。
5. 结果实际到达后 Root 才执行 `verify` → `register`/原 Registry 独立观察 →
   `issue`。只向手机交付 issued 的 bootstrap；operator 和维护凭据不下放。
6. 原调用方下发第二个独立能力请求并读完整结果，然后验收本人选文件、已签名
   数据更新、拔 USB/断网重连。WorkScheduler 注册与真实后台 callback 分别记录，
   无 callback 就保留待验收，不宣称永久后台常驻或 HA。

具体剩余依赖是已签 HAP 的实际安装、真手机首启 ID，以及原 authority 的
实际入网 task/epoch/request 绑定。服务侧只读准备已具备上述证据；不能靠
服务在线、已有 SDK 构建或原开发任务 completed 跳过这些真实步骤。
