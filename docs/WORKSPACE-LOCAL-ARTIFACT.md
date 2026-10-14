# 本 authority 的完整原产物

`MeshWorkspace.artifact` 现有本地调用分支可使用 owner 安装的只读
`LocalArtifactProducer`。这是源码及隔离真实 journal/command 验证，
**不等于生产已安装或原 Leader 已消费**。既有远端 A2A 格式、消息
映射、原指纹与读取接口不变；本地读取不能伪造一条远端消息来满足它。

## 原始来源与只读边界

本地 manifest 使用 `mesh-local-private-artifact-transfer/1`，明确
`source_kind=local_allocation`。它保留 authority、原 task/node/epoch、
完整 allocation plan SHA、operation/capability/epoch/provider/receipt/
nonce、scope/workload/context/settlement SHA、原文件与正文的完整
字节数和 SHA；**没有 sender/message_id/message_fingerprint**。

Workspace 先核对原不可变 intent 和同一私有 journal 中的原
TaskCapabilityCalls intent/creation epoch/hash/reserve attempt，再由固定
reader 读原 authority/provider 账本。它不构造新的 TaskCapabilityCalls、
Store 或 Provider，不发 RPC、不重新准入/续租、不调用 execute。
当前 task、能力租约或 adapter 执行授权已过期，不会触发原业务重跑。

旧薄入口 journal 没有 Workspace intent 时，artifact-only 兼容分支
按原 task+operation 精确取原行，核原 call_identity、整个 intent
canonical bytes/SHA 和原 epoch，返回 `workspace_intent_present=false`、
`intent_source=task_capability_calls` 与原完整 intent SHA。不创建
Workspace 表/行、不合成历史，不按 operation 模糊匹配其他 task。
正常的新 `call` 才延迟初始化其真实 Workspace intent。
CLI 的 `status`/`artifact` 显式使用 readonly existing-journal 绑定：
不存在的文件/表拒绝，不创建/迁移/recovery。已有本地状态使用原
TaskCapabilityCalls readback 校验和只读 inspect，不运行其写构造器。

producer 使用 `mode=ro` 和 `query_only`，核原 completed allocation/
dispatch、原节点 actor、原 epoch/plan、settled provider context/nonce、
完整 canonical settlement 和已存在的原文件。unknown/unsettled、缺行、
身份/范围/哈希变化均拒绝；只剩 SHA、摘要或 provider 自报不足。
它允许实际在本 authority 执行的 A2A 来源 task，但仍只声明
**local allocation**，不将 source task 的来源冒充新的远端委派。

完整文件和 base64 正文都验证后，沿用原非覆盖 atomic spool：私有
0700 目录、0600 文件、完整 EOF、大小/双 SHA、fsync、exclusive link、
落盘后核文件和目录。已有相同文件幂等读取；冲突文件不覆盖。
`artifact_content_verified=true` 仅证明实际完整字节和正文匹配，
`execution_verified`、托管 read grant、内容真实性/账号/模型证明仍 false。

## 固定只读工具与独立候选

2026-10-14 新增 [local_artifact_tool](../assistant_mesh/local_artifact_tool.py)，
不再要求安装者逐次手改 wrapper。它不读/复制 worker 认证，不构造
Client、Store 或 provider，不发 RPC。私有
[source recipe](../deploy/local-artifact-source.json.example)仅引用原文件：

```json
{
  "format": "mesh-local-artifact-source/1",
  "owner_config_path": "/PRIVATE/original-producer-owner.json",
  "owner_config_sha256": "PINNED_ORIGINAL_OWNER_CONFIG_SHA256",
  "journal_path": "/PRIVATE/original-calls.sqlite"
}
```

recipe 是 0600/0700、worktree 外的安装输入，其完整 SHA 由安装者 pin。
原配置是 local 或旧 A2A producer 均可：authority/原 adapter/provider/DB
绑定沿用；实际 local node 从原 `call_identity` 精确提取，再与原 intent、
allocation 和 settled provider 核对。不改原配置的 sender，不伪造
message_id，不补写 Workspace 历史，也不复制配置内的认证。

既有 source owner 文件沿用原 ArtifactProducer 的 owned 0600/0700、
single-link、精确 SHA 合同。某个祖先是 dotfiles Git 根不要求迁移/改写
原私有配置；新的 recipe/candidate 仍须 worktree 外。真实首次 stage
曾因把 Workspace 的 ancestor-worktree gate 加到既有引用而拒绝，
该失败保留；改为复用原 producer 合同，没有放宽原 Workspace CLI 或
宿主权限。

由 Root 在新 owned 0700 空目录生成独立候选：

```sh
python3 -m assistant_mesh.local_artifact_tool stage \
  --spec /PRIVATE/source.json --spec-sha256 PINNED_RECIPE_SHA256 \
  --candidate-dir /PRIVATE/new-candidate --python-executable /usr/bin/python3
```

候选包含 source-only `local-artifact.pyz`、`reader-binding.json` 和最后发布
的 `READY.json`，另有固定私有 `artifacts/` 空缓存。逐个非覆盖原子发布/
fsync，完整 source 文件 SHA、bundle/binding SHA 保留；相同字节可重读，
冲突文件永不覆盖。中途失败可能留下完整候选文件，保留它们并对账，
不用新 operation/重执行业务修复。生成候选不是安装、注册或运行服务。

pyz 仅包含库源码和许可证，不包含原 owner 配置/DB/认证/原文件正文。
其入口固定 recipe 路径/SHA/cache，reader binding 明确 `local_task`，
child 使用安装者选定的 Python `-I`；不改变宿主 HOME/CODEX_HOME、
Shell、网络、MCP 或模型设置。目标有不同环境时由该节点 agent 适配
同一三 ID/full-bytes 合同，本 Python/POSIX 包不是手机/Windows 的要求。
绑定是 node-local 的绝对路径，不是可任意移动的安装包。Root 若应用
到另一个稳定目录，先在该目录用同一原来源生成新候选并核对应 SHA，
不要直接移动旧包、覆盖旧 reader 或恢复旧 DB。

### 只读预检与明确消费

```sh
python3 -I /PRIVATE/new-candidate/local-artifact.pyz inspect \
  --task-id ORIGINAL_SOURCE_TASK --operation-id ORIGINAL_OPERATION \
  --capability-id ORIGINAL_CAPABILITY

# 只有 Root 审查/应用后，由原 native Leader 的明确只读任务执行：
python3 -I /PRIVATE/new-candidate/local-artifact.pyz consume \
  --task-id ORIGINAL_SOURCE_TASK --operation-id ORIGINAL_OPERATION \
  --capability-id ORIGINAL_CAPABILITY
```

`inspect` 核实际原文件的完整 raw bytes/base64 正文/双 SHA，不落正文或
新 journal。它输出原完整 intent SHA/row SHA、整个 calls/provider journal
的 schema、所有表完整行指纹及文件 identity 的前后观察，不以 COUNT
替代。范围明确不包含并行工作的全 authority/native history，不能用
全系统静止要求阻断无关任务。64 MiB 的逐 journal 指纹预算只适用于
这个参考工具，超界要显式适配，不能拿不完整输出称全行一致。

`consume` 只原子回收已验证原文件到固定缓存，保留原 task/op/cap/epoch
及 `workspace_intent_present` 真值；没有重新准入、预留、续租、GET 或
结算。不同 runtime 消费任务与原 source task 要分别记录。源码、工具
自检和 Source 行不变不等于原 Leader 已消费；仍需实际 native 调用/
完整 read/decode、Root 独立源/目的字节与原状态核验。

2026-10-14 已生成临时私有候选，并对真实原校园产物运行了 `inspect`：
完整147280B/正文109356B双 SHA核验；原 calls/provider journal 的 schema、
文件 identity 和完整行前后相同，旧配置/reader保持；cache为空，没有
真实 `consume`、生产应用/注册或新 native model。新7项隔离测试含真正
fixed zip child/CommandArtifactReader与完整153600B正文传输、old A2A
配置原字节保持、全行而非COUNT变化、unknown/pin/路径错配不消费、
候选幂等/冲突不覆盖及 binary error 不混入 JSON。
聚合相关104项/2.489s/0skip通过；首轮新fixture的1failure/2errors源于
producer wrapper缺少read接口，真实stage的已知拒绝也保留，不改为成功。

## 最小增量安装

先由 Root 审查并冻结独立 reader/Workspace 库，不替换正式 Worker、
Store、provider 或旧 receiver，不覆盖旧 journal/config/artifacts。
新 producer owner 配置在 worktree 外 0600/0700，精确 SHA 由安装者 pin：

```json
{
  "schema": 1,
  "authority": "ORIGINAL_AUTHORITY",
  "node": "ORIGINAL_ALLOCATION_TASK_NODE",
  "authority_db": "/PRIVATE/original-authority.sqlite",
  "max_artifact_bytes": 25165824,
  "bindings": {
    "ORIGINAL_CAPABILITY": {
      "adapter": "campus",
      "provider": "node:ORIGINAL_PROVIDER",
      "provider_journal": "/PRIVATE/original-provider.sqlite",
      "owner_config_path": "/PRIVATE/original-adapter-owner.json",
      "owner_config_sha256": "PIN_ORIGINAL_ADAPTER_CONFIG_SHA256"
    }
  }
}
```

这些是占位符。`adapter` 只支持 `campus`/`https`。node 是**原 allocation
actor 节点**，不凭当前任务迁移后的 node 替换。旧 epoch 产物必须保留
对应原 adapter 配置；不能把新的 capability epoch 配置强套到旧文件。

固定 local wrapper 从现有
[三 ID reader 模板](../deploy/workspace-artifact-reader.py.example)改装：
这是保留的低层接口示例；日常优先使用上面的生成工具，不手工改装。
将 import/constructor 改为 `LocalArtifactProducer`，pin 新配置与冻结
库，明确接收 `{task_id, operation_id, capability_id}` 三项安全编码。
模式由已审查的程序决定，不是模型可注入参数。Workspace reader
私有配置加 `reference_kind=local_task`；默认远端继续使用原
`message_id` envelope，不借旧字段假造本地 provenance。程序 API 为：

```python
from assistant_mesh.artifact_transport import LocalArtifactProducer

producer = LocalArtifactProducer(FIXED_CONFIG, EXTERNALLY_PINNED_CONFIG_SHA256)
manifest = producer.manifest(ORIGINAL_TASK, ORIGINAL_OPERATION, ORIGINAL_CAPABILITY)
producer.write_to(ORIGINAL_TASK, ORIGINAL_OPERATION, ORIGINAL_CAPABILITY,
                  binary_stream=PRIVATE_BINARY_STREAM)
```

Workspace 私有 owner config 的 `artifact_readers[ORIGINAL_AUTHORITY]`
安装这个 local 固定命令：manifest/read argv 只保留一个完整
`{request_base64url}` 参数，argv executable、script/config/source 路径及
最大 60 秒的 child 生命周期由安装者固定。可直接本机执行，也可走
既有 owner 原生 transport；无需新增公网 API、token 或模型认证。
固定命令必须真实只读，安全编码本身不证明任意程序无副作用。

```json
{
  "reference_kind": "local_task",
  "manifest_argv": ["/usr/bin/python3", "/PRIVATE/local-reader.py", "manifest", "{request_base64url}"],
  "read_argv": ["/usr/bin/python3", "/PRIVATE/local-reader.py", "read", "{request_base64url}"],
  "timeout_seconds": 30
}
```

## 真实原文件消费验收

在原来源确认已存在的 operation、task/node/epoch、provider settled
record 和对应 adapter 配置，不重新 GET、不换 ID。安装后只执行：

```sh
python3 -m assistant_mesh.workspace_cli --config /PRIVATE/workspace-owner.json \
  --task-id ORIGINAL_SOURCE_TASK --epoch CURRENT_SOURCE_TASK_EPOCH \
  --operation-id ORIGINAL_OPERATION artifact
```

不要在旧 journal 塞入“以前由 Workspace 执行”的假 intent。若原调用
仅来自旧薄入口而没有 Workspace intent，CLI 沿用该原 calls journal
的只读兼容入口，保留真实来源；缺行不是重跑许可。
新 runtime 消费任务与原 source task 是不同身份时必须明确记录两者，
不能把 CLI 的原 source ID 冒充新任务的执行权限回执。

由原 native Leader 实际调用 reader、读取目的端完整文件并解码/核 SHA，
Root 再独立比较原文件/目的字节与 body，核原 provider row/count/nonce
及旧历史/unknown 保持。这一步才证明生产消费；源码测试、路径、
进程存活、manifest 或 SHA 本身都不能代替它。

## 本次验证

17 项新聚焦测试使用真实隔离 allocation/provider/TaskCapabilityCalls
账本及合成 HTTP 回复，另有真正固定 Python child/现有 CLI 传完整
153600 B 正文。覆盖无 A2A origin、无 RPC/constructor/再 GET、原始
三个 journal 全部行不变、旧 epoch/过期执行配置读回、unknown保持、
缺表不创建、intent/节点/任务/plan/scope/workload/正文错配、truncation、
假远端字段、冲突不覆盖及重开幂等、legacy原完整行/schema/inode/文件
保持、明确 task_id 固定命令、readonly缺库/缺表不创建及新call延迟初始化。
聚合验证结果另在交接记录，不以计数替代生产 local 闭环。

本 Python/私有文件参考实现不强迫 Windows、手机或临时云环境采用
相同安装。其它平台可以实现相同原身份/full-bytes 合同；Mesh 不阻断
节点任何原生 Shell、文件、网络、浏览器或 MCP。
