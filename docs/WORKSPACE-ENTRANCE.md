# 原身份的统一能力入口

`MeshWorkspace` 是既有能力准入、A2A 委派和原产物读取的薄组合，不是
第二个 Leader、全局共识或拦截原生功能的代理。模型仍可直接使用
Shell、文件、网络、浏览器和其他 MCP；本入口只管理 Mesh 自己提供的
共享能力。源码与下面的隔离验证不等于生产已接通。

实际接线续接：2026-10-12原Leader同thread已真实运行CLI discover并
完整读取15347B、8section无截断盘点。23:20原唯一receiver实际加载
两只读指纹投影，原Worker/10个保护表和历史保持。校园租约过期已
定位、恢复tool任务仅一次accepted；新call/delegate/full-artifact与
实际指纹仍未闭环，local-artifact分支仍欠。详见
[状态](STATUS-2026-10-12.md)，不以这段新增替代下面的授权/未知边界。

## 模型使用的四个动作

| 动作 | 行为 | 不能据此推断 |
| --- | --- | --- |
| `discover` | 原 task/epoch 的能力、作用域、TTL、单位、容量池和观测投影 | 目录中的声明已经可执行 |
| `call` | 本 authority 组合原 propose/reserve/link；远端组合原 propose/A2A delegate/link | 新授权、独立执行证明或全局 exactly-once |
| `status` | 按原 operation/decision/child 查询原回执 | 超时可换 ID 重派 |
| `artifact` | 已安装只读 reader 时取回完整原产物、核绑定与全字节 SHA | 内容正确、远端副作用已闭合或文件已经公开 |

模型选择具体 issuer、capability instance/epoch、action、scope、workload、
candidates 和 rationale。私有安装决定 authority/node、现有认证 Client、
journal 和可用 reader；这些不能由模型调用参数改成另一身份、URL、
文件路径或执行器。保留实例差异、目标作用域、共享瓶颈和故障域，
不把同类能力当作相同且独立的容量，也不硬编码永久模型排名。

远端子任务复用指定 project/agent 类别，请它调用自己的本地薄入口；
父任务保存原始 wire message，升级后的 prompt renderer 不回写历史。
原委派和实际 A2A delivery 的两项 fingerprint 都必须与这条原消息
一致。只有相同 IDs 不能证明相同输入；缺投影或不匹配保持 unresolved。

每个 operation 的不可变 intent 与 propose/delegate/link CAS 阶段保存在
私有 SQLite journal。写入尝试失去 ACK 后只观察原状态，不自动再次
调用；更换模型、版本、节点或 task epoch 都不能重放未知效果。
新 epoch 不启动尚未尝试的旧阶段。journal 必须随安装升级保留。

## 可选 native CLI

源码 [workspace.py](../assistant_mesh/workspace.py)、
[workspace_cli.py](../assistant_mesh/workspace_cli.py)。CLI 独立于原 Worker，
不替换其 tool dispatcher、历史或默认模型。

把 [owner 模板](../deploy/workspace-owner.json.example) 适配到仓库外
owner-only 配置，填写已有私有 worker config 的精确 SHA、原 authority/
node、私有 journal/artifact 目录。文件 0600、目录 0700，不允许 symlink、
重复 JSON keys 或公开 worktree 内的私有配置。参数文件同样私有。
以下是安装后形式示例，不是已执行的生产命令：

```sh
python3 -m assistant_mesh.workspace_cli --config /PRIVATE/workspace-owner.json \
  --task-id ORIGINAL_TASK --epoch ORIGINAL_EPOCH discover

python3 -m assistant_mesh.workspace_cli --config /PRIVATE/workspace-owner.json \
  --task-id ORIGINAL_TASK --epoch ORIGINAL_EPOCH \
  --operation-id ORIGINAL_OPERATION --payload-file /PRIVATE/selection.json call

python3 -m assistant_mesh.workspace_cli --config /PRIVATE/workspace-owner.json \
  --task-id ORIGINAL_TASK --epoch ORIGINAL_EPOCH \
  --operation-id ORIGINAL_OPERATION status

python3 -m assistant_mesh.workspace_cli --config /PRIVATE/workspace-owner.json \
  --task-id ORIGINAL_TASK --epoch ORIGINAL_EPOCH \
  --operation-id ORIGINAL_OPERATION artifact
```

call 参数文件仅允许 issuer、decision_id、selection、candidates、rationale、
lease_seconds、project_id、agent_id。task/epoch、owner、transport 和 reader
不在可注入参数里。CLI 对未知、缺原回执、缺 reader 返回诊断/非零退出，
不重新运行原 HTTP/model/tool。discover/status 会创建或读取自己的私有
journal，但不实例化 authority Store 或触发其恢复/migration。

## 完整产物与 native/SSH reader

reader 是 owner 安装并审查的只读接口，manifest/read 都只接原 message、
operation、capability 三个 ID。原产物的 authority/sender、A2A message
fingerprint、task、capability epoch、scope/workload hash 先核一致，再核
完整 binary EOF、大小、文件及正文 SHA，私有原子落盘。没有 reader
不能退化成“只收摘要就当完整成果”。当前本 authority 的 artifact 路径
尚未接入；其调用/状态可用不意味着本地产物自动回收已实现。

[reader 模板](../deploy/workspace-artifact-reader.py.example) 从独立冻结的
ArtifactProducer 读取已完成原效果，不创建 Store、grant 或重发业务。
固定 argv 唯一整项 `{request_base64url}` 接三个 ID 的安全编码，避免
将模型原字符串插入 SSH 的远端 shell。manifest 上限 16 KiB，完整
artifact 沿用原协议上限；超时只回收拥有的本地 child。

**安全编码不是任意命令沙箱。** 固定 executable、SSH config/include、
alias、host key、远端 reader/source 和 owner config 仍是安装者信任。
当前 pin 已有 Client 配置 SHA 和 reader 使用的 ArtifactProducer owner
配置 SHA，不自动证明其他命令文件无副作用。上线前按实际环境审查
这些绑定；本地 timeout 不证明远端进程已经退出。本只读 reader 不应
执行新的网络业务或模型推理，这与原生 agent 任意 Shell 的权限无关。

## 冻结旧核心与验证范围

原 3198 核心只需两项 routing/routing_context 指纹投影，不升级 Store、
Worker 或 schema；精确旧版本验证见
[兼容说明](WORKSPACE-LEGACY-PROJECTION.md)。没有实际加载投影就不能
将源码测试当生产可用，也不能为补字段重派旧子任务。

2026-10-12 Root 和独立专员各跑 CLI 11、workspace 14、精确 legacy 6，
共 31 项、无 skip。覆盖原 write CAS/失去 ACK/epoch 与输入冲突、旧
renderer 续读、完整 153600 B 产物，以及实际拥有的 Python reader child
传输完整二进制/安全编码/大小限制/私有配置/秘密错误脱敏。legacy
检验原 Store/Worker source bytes、只读 schema 和所有原行保持。

这些是隔离 authority/command/protocol 试验。尚未据此声称原 Leader
生产 CLI 使用、远端自动产物接线、Chat/官方 Cloud 入网或整张 Mesh
自治完成。环境适配合同可复用；本 Python/private-storage 参考实现
不是要求 Windows、手机或临时云节点使用同一安装方式。
