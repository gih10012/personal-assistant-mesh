# 共享容量与 provider 回执

这是一条额外的受管 Mesh 合同，不是终端执行代理、原生工具白名单或网络门禁。模型仍可直接使用原生 Shell、文件、网络、MCP 和自行创建的工具。目录和预留都不证明能力已经执行。

## 已接通的链路

模型选择能力/路径 → 新鲜独立观测与 exact scope/action 授权 → 同一权威 SQLite 事务预留共享容量 → provider 接收回执 → 启动许可回执 → 未知状态或结算报告。

- owner 定义容量池及能力到池的绑定。dimension、unit、workload、metric 和能力 kind 都是开放字符串/结构，不按固定设备等级或硬件类型排序。
- 同一物理资源不能因多个能力公告而重复增加容量。owner 的 `usage_key` 定义同一 allocation 中哪些声明属于同一物理使用；相同使用取最大需求，不同使用相加。
- 模型的 selections 必须带当前能力 epoch、精确 scope/action、workload 及有单位/时效/独立验证者的 observation。可选 route 逐跳核对边、端点版本和可达证据；不是套用固定机器路线。
- `reserve` 在同一事务核对实际任务 epoch、Leader 任期、能力/授权/池/绑定/观测和可用容量，然后写入稳定 operation ID 和每个 provider 的待接收 journal。
- `accept`、`start` 再核对任务、Leader、全部资源版本和证据。稳定 receipt ID 防重；重复 `start` 返回 `execute_once=false`，不能因此重新执行。
- 未接收的预留可以超时释放。accepted/running/unknown 不因 TTL、撤销、任务完成或 Leader 更替而自动释放；它们可能仍在消耗资源或已经产生效果。
- 原 provider 在任务/Leader 租约失效后仍可 `inspect`、报告 unknown 或有实际静止证据的结算；这是核对旧结果，不是开始新效果。未接收的另一跳可以 `decline`，不能冒充已接收任务的结算。
- 结算证据要求 `resource_quiescent=true` 和 `result_reference`，当前只标记 `provider_reported`，不标记独立验证或真实执行完成。
- 默认新增支出为零，当前 binding 拒绝非零 `new_spend_minor`，不会购买资源、额度或重置次数。

## Agent 与操作入口

新 native thread 的额外工具：

```json
{"action":"allocation","arguments":{"action":"reserve","arguments":{"operation_id":"stable-operation-id","plan":{"target":"selected-capability-id","selections":[{"capability_id":"selected-capability-id","epoch":1,"action":"compute","scope":{"project":"example"},"workload":{"shape":"example"},"observations":[{"observation_id":"actual-verified-measurement-id","metric":"throughput","unit":"items/s","max_age_seconds":60,"predicate":{"op":"gte","value":1}}]}]}}}}
```

`reserve` 的 task_id/task_epoch 由实际 Worker 注入，模型不得覆盖。该示例是合同形状，不是已登记能力、授权或执行适配器；观测、workload 和 scope 必须与实际账本一致。

旧线程不为新工具重建或换写上下文，可用原生终端的私有文件入口：

```bash
python3 -m assistant_mesh.cli --config /PRIVATE/worker.json allocation --action inspect --payload-file /PRIVATE/arguments.json
```

配置和 payload 都放在 Git 外本人所有的私有文件中，不在 argv 传 token。CLI reserve 需当前任务 task_id/task_epoch，authority 再与凭据所绑定的 node 校验。API 为 `POST /v1/allocation/action`，身份永远来自认证，不接受 JSON 冒充 actor/provider/operator。

owner 操作：define_pool、renew_pool、revoke_pool、bind_pool、expire。认证节点可读其允许范围及使用 reserve、inspect、pending、accept、start、unknown、settle、decline、cancel；core 再校验 provider 或原 actor。viewer 没有此变更入口。

## 宿主安装的 provider 执行桥

`assistant_mesh.provider.ManagedProvider` 提供可由宿主安装的 callback 桥：owner 绑定固定 handle、capability epoch、action 和 Python callable；模型的 plan 不能安装/替换它，也不会作为 Shell 源码执行。scope/workload 在调用前再与认证 authority 的实际 plan 核对。

provider 使用独立私有本地 SQLite journal，绑定稳定 authority/provider 身份；同 operation/capability 使用文件锁。accept/start/invocation intent 在对应 RPC/调用前落盘，只有本次新 `start.execute_once=true` 才首次调用 callback。重启、请求超时、active/unknown marker 都不会自动再调用。实际 callback 的结果和静止声明先落盘，随后才结算；结算响应丢失可按原 ID 重报已记录的结果，不重做工具操作。

```python
from assistant_mesh.provider import Adapter, ManagedProvider

# owner_callback 是宿主自行安装的函数，不从模型输入 eval/import。
provider = ManagedProvider(provider_client, "/PRIVATE/provider.sqlite", "node:cloud",
    {"installed-capability": Adapter("installed-version-1", 1, "compute", owner_callback)},
    authority_id="stable-owned-authority")
provider.run_pending()
```

callback 必须自行实现有界执行，并返回实际 result_reference 与 resource_quiescent；异常、超时或 PID 消失都不能代替它。`local_invocation_intent_recorded` 与 `local_invocation_started` 分开，未知 intent 不假报已调用。所有结果仍标 `provider_reported`，独立验证为 false。

首次并发开库只有 WAL 设置的精确 locked/busy 错误可在五秒总预算内
重试；身份/schema 仍先验证，BEGIN、业务写入和 callback 都不重试。
这项可用性处理不把未知效果改为“没有执行”。

## 可选独立常驻运行器

`assistant_mesh.provider_runtime.ProviderRuntime` 从宿主私有 owner manifest
加载已安装工具。它不是 Node 主循环的一部分，不启动模型、通道接收器、
监听端口或自动广告；使用与 authority 凭据绑定的独立 `Client`。
CLI 与仅供选择安装的 user service 模板见
[`assistant-mesh-provider.service.example`](../deploy/assistant-mesh-provider.service.example)。

```bash
python3 -m assistant_mesh --config /PRIVATE/provider-client.json provider --owner-config /PRIVATE/provider-owner.json --action describe
python3 -m assistant_mesh --config /PRIVATE/provider-client.json provider --owner-config /PRIVATE/provider-owner.json --action poll
python3 -m assistant_mesh --config /PRIVATE/provider-client.json provider --owner-config /PRIVATE/provider-owner.json --action serve
```

标准 client 配置只有受保护的 control URL/token 文件入口；owner manifest
独立保存 provider/authority 身份、持久 journal 路径和安装绑定。例如下面
是**合同形状**，占位 SHA 不是已安装的有效工具：

```json
{
  "schema": 1,
  "provider": "node:owned-node",
  "authority_id": "stable-owned-authority",
  "journal": "/PRIVATE/provider.sqlite",
  "poll_interval_seconds": 2,
  "dispatch_limit": 100,
  "reconcile_limit": 100,
  "adapters": [{
    "capability_id": "installed-tool",
    "handle": "owned.tool",
    "version": "1",
    "module": "/PRIVATE/owned_tool.py",
    "sha256": "<ACTUAL_64_LOWERCASE_HEX_SHA256>",
    "function": "invoke",
    "capability_epoch": 1,
    "action": "owner-defined-action",
    "new_spend_minor": 0
  }]
}
```

配置、模块和 journal 须本人所有、普通单链接 0600 文件，直接父目录
0700；部署配置应放在 Git 外。模块使用 UTF-8。完整 manifest、每个
源码 SHA/entrypoint 和 journal namespace 在任何模块顶层代码执行前
验证；构造器和 `describe` 可初始化 journal，**并非纯只读命令**。
启动仅 compile 已验证快照，模块顶层代码/import 与函数调用都延迟到
新的 authenticated admission 之后。每次调用用该快照的独立 namespace；
需要跨调用状态的工具应自己用文件、数据库或独立 daemon 持久化。
Leader 的原生 thread/记忆不受此工具 namespace 生命周期影响。

`describe` 给出可由宿主显式 advertise 的 `managed_adapter` 描述；须
原样放在 capability 的 `spec.managed_adapter`。每次首次回调前再读取
认证 registry，校验 provider、available、实际 epoch 和完整 descriptor。
源码、版本、入口函数、位置（只发布位置哈希）或 action 改变都需要
真正的 capability epoch 升级和新 runtime；不会把新代码冒充旧工具。
这与 agent 用原生 Shell 自主写代码、测试、部署、创建新能力完全兼容：
“owner-installed” 指宿主的受信任安装边界，**不是**要求每段代码都
由人手工创建，也不是 native Shell/MCP/联网的准入白名单。远端 plan
只选择已广告合同，不将 prompt 字符串直接作为安装源码执行。

轮询只给新 admission 执行一次。已记录实际结果的
result_ready/settlement_intent/settlement_unknown 可在联网恢复后按原 ID
补结算；active/unknown/intent 而无实际结果的记录不会重放、清库或
虚报容量释放。旧版尚未接收的请求遇到不同安装 epoch/action 时单独
报告 degraded，不挡住本批次新合法请求和旧结果补结算，也不自动
decline/cancel 旧请求。超过 dispatch_limit 的大量旧项仍可能造成
head-of-line 排队；owner 可对确认未接收的原 dispatch 显式 decline，
或等待正常未接收过期，不能对 accepted/unknown 套用此处理。

如新模块损坏/丢失导致构造失败，旧结果仍不能伪称丢失：可用同一
provider/authority/journal 的独立 **`adapters: []` manifest** 补结算，
不加载缺失模块或重做旧工具。manifest 坏 schema/身份、journal 损坏
和认证拒绝均不是健康状态；暂时网络不可用则保留状态继续轮询。
SIGTERM/SIGINT 只在调用之间停止新工作；已进入的回调仍须真实结束、
记录结果。强制终止、异常或退出进程都不是静止证据。

Python 模块是 owner trust，**不是封闭供应链、CPU/费用沙盒或进程隔离**。
依赖未全部 hash 固定，同 UID 原生程序仍有自己的 OS 权限，registry
检查与回调也不是分布式原子事务。回调可阻塞甚至退出整个 provider
进程，所以采用独立进程而不嵌入 Node 的心跳/重连生命线；有界执行与
真实 quiescence 必须由具体工具实现。service 模板未自动安装，普通
Node/Worker 启动不启用该运行器。

## 隔离实际执行验收

`scripts.probe_provider_execution` 是 owner 显式分阶段运行的验收器，
不启动模型、服务或 SSH。先在本人已有 0700 `/var/tmp` 根中 `prepare`，
将同一 bundle 放到两端同一绝对路径，但 **owner-reference.json 只留
owner 端**。随后 owner 显式启动无 iLink 的独立 authority，执行
`submit`，由原 requester credential heartbeat/claim 唯一固定父任务，
再按真实 task/Leader epoch `reserve`，在 provider 宿主 `execute`。

默认 `observe --root PRIVATE_ROOT` 只读 API 与闭合 provider journal，
核对实际 SHA、原 operation/receipt、结算和容量释放；不构造 bridge，
不创建 state/journal，不补 SHM。SQLite reader 须 >= 3.22；旧节点可以
execute，闭合 journal 可复制到新版节点验收。`reconcile` 必须显式使用
原 ID，仅重报已保存结果，不再调用 callback。超时不换 ID；丢失 state
也不能当作没执行。持久记录和完整 SQLite 文件应另作私有备份。

这里独立观察的只是实际 64-byte 输入与结果参考，不是延迟、吞吐、
CPU/GPU 强制配额或全网最佳调度。单 slot pool 是 owner 的逻辑 admission
合同，不是机器级资源限制；该验收也不证明模型自主选择/安装了工具。

## 必须继续实现和验收

可选运行器未默认接入 Node，也没有自动创建原账本子任务或执行未批准的远端模型源码。跨宿主 SHA callback 已真实验收，运行器的实际常驻部署和模型自主安装/组合调度仍须另验收。下一段须继续各节点实际工具版本升级、独立性能验证、进程/产出核对和组合路径逐跳失败恢复。请求超时不证明未执行，不能换 operation ID 重做。

尚未宣称：分布式多权威容量一致性、真正最佳调度、实时 CPU/GPU 强制配额、迁移未决外部效果、独立验证 provider 的结果。真实计量和资源适配器也不能仅凭 unit fixtures 或 HTTP 回执标为完成。
