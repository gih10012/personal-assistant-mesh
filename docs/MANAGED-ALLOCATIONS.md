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

## 必须继续实现和验收

现在没有自动创建子任务或执行模型提交的命令。下一段必须把 journal 接入真正的 provider adapter 和效果账本：稳定 ID 的实际 admission/start、活进程/已产出结果的核对、静止证据、独立结果验证，以及组合路径的逐跳失败恢复。请求超时不证明未执行，不能换 operation ID 重做。

尚未宣称：分布式多权威容量一致性、真正最佳调度、实时 CPU/GPU 强制配额、迁移未决外部效果、独立验证 provider 的结果。真实计量和资源适配器也不能仅凭 unit fixtures 或 HTTP 回执标为完成。
