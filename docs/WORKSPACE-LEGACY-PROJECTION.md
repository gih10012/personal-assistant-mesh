# Workspace 对原3198 authority 的最小只读投影兼容

新 Workspace 的 unknown 恢复必须核对**真实原委派内容**，不能只对上
child/message ID。`routing_context` 的 remote records 与 `routing` 的
linked remote reference 现在额外返回：

- `delegation_fingerprint`：原 `remote_delegations.fingerprint`。
- `message_fingerprint`：按原 peer/message 关联的 `mesh_deliveries.fingerprint`。

两字段都在
`3198c877234a8fd0c6912fdbe218f47185effedc` 的 Remote/Network 原表中；
只读 LEFT JOIN 无 schema、Store、Worker、native goal/history 或权限变化。
缺 delivery 时返回 null，Workspace 保持原 unknown，不重派。原有
caller/task/epoch 栅栏和资源准入均不放宽，投影不等于执行授权/验证。

## 最小文件边界

相对于原3198，仅 `assistant_mesh/routing.py` 的 `_reference` 和
`assistant_mesh/routing_context.py` 的 `read` 改 SELECT/返回字段，当前
diff 为13行新增、5行删除；无需新依赖或替换整个 server/Store/Worker。
此数量是该切片证据，不是未来改动的永久约束。

receiver 的已部署最小归档 `116bacc2…da01` 与常驻 Worker 仍是各自
发布边界。若需要给它所在 authority 增加这两项投影，应从其精确冻结
源码制作**新私有 candidate**，仅附两模块和实际测试，不原地覆盖运行
源码，也不借机加载候选 goal/模型/runtime 功能。独立 CLI/Workspace
可以用自己的发布包，authority 必须确实加载 additive 投影；目录存在
或 client 新版本并不代表旧 process 已经有这些返回字段。

装载/切换是 Root 后续单独受控生产步骤，本片没有做。不能为只读检查
在生产 DB 上实例化 `API`/`Store`：原3198构造器的 schema/inflight
recovery 行为仍存在，可能重分类无关 submitting/steering。必须保留
原账号、账本、43任务、会话/unknown 与 native Worker；唯一 receiver
重启会暂时影响 authority 可达，不能把“不改 Worker 文件”误作原生
活跃 turn 绝不会受影响。选择实际安全边界，不启动第二 authority/
receiver、不恢复旧 DB，不停止无关工作。

## 独立源码兼容证据

`tests/test_workspace_legacy_projection.py` 从精确3198 git object（或显式
`PAM_LEGACY_BASE_ROOT`）加载整个旧 package 的独立内存 namespace，只
叠加上述两模块。旧 source 缺失明确 skip，不以现代 Store 代替。
Store/Worker 原 hash 分别为
`7d8f750e…440` / `816a8eb2…bf0`；测试核对每个实际加载的非投影模块
逐字节仍是原 source，并用 AST 证明只两项 read method 改动。

隔离 fresh ledger 上验证新 Workspace 原 propose/delegate/link、丢 ACK
只查原 fingerprint、缺 delivery 保留 unknown，以及 context/inspect
前后完整 schema/全部 row 不变。所有测试写入只发生在新的 fixture，
不连接生产或外部模型。不把这些源码兼容测试称作 authority 已升级、
两端真实能力执行、完整 artifact 回收、HA 或异构设备已入网。
