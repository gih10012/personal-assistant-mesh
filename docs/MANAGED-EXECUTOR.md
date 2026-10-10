# PAM-004c：独立受管 executor

Mesh 的 provider 是额外能力入口，不限制原生 Shell、文件、联网或 MCP。
Leader 根据真实能力、范围、证据与容量选择并 reserve/link；独立 executor
消费已准入 pending，保存原执行 journal，回报真实结果及结算。不要把
provider 内嵌到 Leader/Node 心跳或让 Leader 生命周期清掉执行记录。

## 最小复用方式

复用 [provider_runtime.py](../assistant_mesh/provider_runtime.py) 的 `serve`、
已有 owner manifest、原 provider/authority/journal 和唯一版本化 callback。
[user service 示例](../deploy/assistant-mesh-provider.service.example) 是 Linux
部署示例，不要求 Windows/手机复制 systemd；节点 agent 按环境改装运行
方式，身份、原执行记录与 unknown 不重放合同保持。

普通 `provider serve` 会尝试该认证 provider 的整个 pending page，不是
单 operation executor。首次启动前审查所有安装 bindings、原 journal 和
整个 pending page；不能把构造 runtime 或 `provider describe` 当纯只读，
构造会初始化/打开执行库。模块 byte/hash/编译检查可以单独纯读取，不
import/执行 callback。能力目录传播和 service active 都不是调用完成证明。

原 journal 中 invocation intent/unknown 无实际结果时不自动调用；已有
真实记录结果可以按原 ID 补 settlement，不重新执行。SIGTERM 停止新
dispatch 并等待当前 callback；强制死亡或 PID 消失不是 quiescence。
模块、manifest、结果、私有客户端和 token 不进公开仓库，也不把 operator
bearer 交给普通入口。service 无新权限、新花费或原生工具 allowlist。

executor 不自动延长 owner 容量租约、能力租约或独立观测的 TTL。失效
时仍需有来源的 fresh 证据与 owner 授权，不把一次固定 GET 扩成通用
联网或性能证明。新版本、新 journal、新 node 不是旧未知效果的重试许可。

## 实际 cloud 加载（2026-10-10）

Root 在个人 VPS 核对原 manifest/模块 bytes 和编译绑定、原 canary 的
闭合 journal/独立产物/结算，并确认整个 pending 为空。完整旧 journal、
owner manifest、Git oracle、真实 artifact 和核验记录保存于私有目录。
这是只读快照；仍使用原 journal，不删旧行或把快照恢复成新执行账本。

独立 `assistant-mesh-provider.service` 已启用并 active/running，
`NRestarts=0`；首次报告 `status=ok,dispatch_count=0,local_unsettled=0`。
原 canary 行逐字段保持，原 provider/authority/安装 binding 不变；仅增加
独立 service，Leader/ClawBot/cloud companion 保持 active，未重启它们。
私有安装 helper 的 6 项失败/幂等/保留 fixtures 在本机和 VPS Python 3.6
均通过；这些 fixtures 不是实际自动执行证明。

这完成了加载/空闲验收，尚不能仅凭 PID 声称自主执行。原容量租约过期后
owner 用真实原 pool epoch CAS 重建相同容量合同并更新原 binding；独立
观测重新实际采样，旧观测不续期。随后新的明确工作
`PAM-004c-executor-handoff-20261010-v1` 已提交，要求原 Leader 原生 thread
沿用连续记忆、生成真实 plan、自行决策/预留/关联新的
`PAM-004c-executor-get-20261010-v1` 并等待现有 service。
原 `PAM-004b` 任务/operation 完成记录保留，新 trial 不是其重放。

首次权威回读观察到新 task 在原 Leader thread epoch 1 running；之后
原 task 在同 thread/turn `01a123cf-c025-7fc2-ab9c-8de8f70253e4` completed。
模型决定 `PAM-004c-executor-choice-20261010-v1` 的选择与真实 plan 精确
一致，并绑定原新operation。service 报告 dispatch_count=1 后回到空闲
ok；新operation/dispatch completed、池 epoch 2 held=0/remaining=1。
Root 没有手动 execute，没有新 journal；旧 canary 行未变，当前原
journal 两条真实执行行均 settled。

Root 独立只读核验新 operation/task/epoch/receipt/nonce/dispatch settlement、
实际 artifact 和本地 Git oracle：正文 10,814 bytes，正文 SHA
`00e502b82056d6f5e0e6eabd1b8505553a42d7b1769a393f1d89f85f258249c8`；
artifact 原字节 SHA
`9cfcb6fd07da6e325994163fbea8c0b381ec40cd52e8644b2bfab263eb3f8c89`，
canonical SHA 与原 oracle 一致。两次请求得到相同固定公开正文不表示
同一次执行：新 operation 的原 receipt/nonce 和独立产物路径均核对。
新 task 完整结果 SHA
`5e67ab90c4714d4a53ff8d4142cde322ff0d5d6a89ece7ad8be3a309d0b4ced5`。

这验证了该单目标的正常自动 executor 消费链，不再需要 Root 逐次启动。
Core 仍为 provider_reported，不升级为性能/物理主机证明。多候选、跨主体
调用、断连下执行对账和长期原生 goal 生命周期仍是后续验收，不从这一
独立 service 推导全局 HA。新 task 的 native plan/goal 仍 null；模型
报告当前工具中没有 update_plan，此为模型报告，实际工具可用性仍须
单独核对，不能用文字计划伪造 native checkpoint。
