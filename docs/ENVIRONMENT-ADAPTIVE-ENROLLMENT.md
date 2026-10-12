# 环境自适应入网：合同而非固定操作系统

Mesh 为原生 agent **增加共享能力入口**，不拦截 Shell、文件、网络、
浏览器或 MCP。失联后原生 agent 仍可自行联网、修复、寻找已授权 peer；
使用新路线不必先注册，核验后再推荐/共享。此合同不替用户新增信任、
购买算力、复制凭据或授权控制其他设备。

## 一份合同，三种运行型

`assistant_mesh.enrollment.validate_enrollment(document)` 是纯 JSON
校验/脱离输入引用，import 和校验不启动进程、不访问文件或网络。
没有 Python、systemd、SSH、POSIX、常驻进程、固定目录或固定模型要求。
其他语言/平台实现同一合同即可，现有 Linux 运行器只是一个参考实现。

```json
{
  "format": "mesh-enrollment/1",
  "mesh_id": "owner-mesh",
  "node_id": "stable-owned-node",
  "owner_binding": "existing-owner",
  "repo_pin": {
    "repository": "https://github.com/gih10012/personal-assistant-mesh",
    "commit": "0000000000000000000000000000000000000000"
  },
  "runtime": {
    "kind": "temporary",
    "expires_at": 1791900000,
    "state_ref": "private:authority-persistent-state"
  },
  "join": {
    "peer_id": "existing-peer",
    "authority_id": "existing-authority",
    "route_ref": "private:existing-join-route",
    "credential_ref": "private:scoped-credential",
    "trust_ref": "private:owner-trust-record"
  },
  "continuation": {
    "project_id": "mesh-self-maintenance",
    "session_ref": "private:original-project-session",
    "state_ref": "private:continuous-task-checkpoint"
  }
}
```

全部身份/引用为示例；全零 commit 是待替换占位符，不是可部署版本。
`repo_pin` 只锁定 owner 选定的来源声明；结构通过不证明下载/checkout
字节、仓库权限、真实设备身份或可执行来源已经独立核验。

| kind | 意义 | 持久性 |
| --- | --- | --- |
| `persistent` | 有条件常驻、本地自主维护 | 状态由节点或 Mesh 保存，期限可为 null |
| `temporary` | 一个 job/短期租期内可用，如官方 Codex Cloud | `expires_at` 必填；状态/原任务留在 Mesh，不依赖 VM 存活 |
| `interactive` | 仅用户会话打开时可用，如 Chat 或手机入口 | 不要求后台心跳/文件系统；空闲不能被当成故障或持久算力 |

`*_ref` 是私有配置句柄：Windows 凭据库、手机安全存储、Mesh 私有状态
或原生会话都可实现；不是必须存在的 Linux 路径，更不能填 bearer/key
值并公告。实际使用时核对 owner 授权、真实 peer 身份及期限。解析器
不擅自判断当前连接、额度或 runtime 是否活着。

Cloud 保留其既有 `task_id/epoch/lease/request/provider_job` 外层绑定，
可以嵌套这个 enrollment；不能把 Alibaba `cloud` 当作 OpenAI Cloud。
`openai-codex-cloud:<job identity>` 的任务与成果在临时 VM 退出后可续查。
此合同本身不提交 Cloud job，也不装 Chat 插件。

## 让节点 agent 自适配，而不是让框架穷举平台

1. 读取有效 owner goal、固定仓库版本和已授权接入合同。先发现现有
   harness、原生工具/skill、网络路线、私有认证引用、可用状态存储。
2. 用当前环境原生能力实现最小客户端/桥。无常驻权限就选临时或交互
   模式；只有 SSH 的设备可由有 agent 的节点代理，不伪装它有模型。
3. 提交 `mesh-adaptation-report/1`：实际 system/harness/storage、候选
   capability `id/revision/actions` 与 gaps。`available` 只是声明，不把
   模型说“已修复”、接口存在、版本号、租约或 ping 当执行证明。
4. 先执行一个 owner 已授权的原身份 challenge，回传完整结果/artifact。
   由可信外层 verifier 对照独立 oracle 全字节核验，再显式登记能力。
5. 仅把验证过且仍有授权的能力推荐给 Mesh。断连后继续本地工作；恢复
   时按原 task/session/operation/result 对账，不合并账本假装共识。

报告的 `native_features_restricted=false` 是 Mesh 的不拦截承诺，不是
“设备必须绕过本机 OS 权限”。有联网但没有可用模型，只报告降级，不
声称脱网推理成功。适配代码仍由原生 agent 编写/测试；协议 JSON 不
远端下载安装源码，也不增加一个控制原生功能的白名单。

## 最小可执行参考与完整成果

Linux 上可信部署方可显式创建
`EnrollmentJournal(state_dir, document, authorization, verifier_id, clock=...)`。
它只使用自己的 `enrollment.sqlite`，不导入/实例化原 `Store`，不迁移或
恢复生产账本。POSIX 0700/0600 是这个参考的私有存储实现，其他平台
应采用等价 ACL/安全存储；不是协议前提，也不修改系统环境变量。

`authorization` 由可信 owner 安装方提供：精确
`enrollment_sha256`、`valid_until` 和已授权 `capabilities`，每项含
`id/actions/source_device/proxy_binding`。这些不接受远端 node 自批。
绑定已包含 owner/mesh/node/repo/join/continuation；更换 route/仓库/
owner/policy/verifier 不能静默重绑原 journal 或解放未知效果。

```python
from assistant_mesh.enrollment import EnrollmentJournal, result_envelope

# document/authorization/state_dir/verifier/executor/registrar 均来自可信
# 安装配置；模型不能用 tool 参数覆盖它们。这里不新增授予或生产注册。
journal = EnrollmentJournal(state_dir, document, authorization, verifier_id)
request = journal.intent(
    'original-enrollment-operation', capability_id, action,
    input_bytes, independently_obtained_expected_bytes, ttl_seconds=60)

# 执行器用当前环境的真实原生机制执行；可用任何 harness/语言。
# executor 接收私有 challenge JSON，返回 result_envelope(request, full_bytes)。
reported = journal.execute(request['operation_id'], executor)
verified = journal.verify(request['operation_id'], independently_obtained_expected_bytes)
registered = journal.register(request['operation_id'], registrar)
full_bytes = journal.read_result(request['operation_id'])
```

状态严格区分 `intent → attempted → result_reported → verified`；登记另为
`not_registered/attempted/unknown/registered`。结果含原 operation、nonce、
request SHA、完整 base64/字节数/SHA、原 continuation 和 source binding。
参考对结果长度、全部字节/哈希及独立 oracle 核验，不把摘要/哈希本身
当完整 artifact 已回收。默认上限：输入 64 KiB，产物 8 MiB；更大成果
可由现有 artifact transport 完整传输，不能截断后宣称通过。

哈希使用参考的确定性 ASCII JSON（排序键、紧凑 separators、Unicode
转义、禁止非有限数值），不是宣称 RFC 8785。其他语言应复现编码或保留
已确认 canonical bytes；合同时间/序号用可精确表示的整数 Unix 秒。

参考 `verified` 仅表示可信外层的**独立挑战字节核验**，不是远程模型/
native 工具/物理设备执行 attestation。`register` 只接受可信 registrar
对原 publication 的准确回执；不会代写 existing Registry grants/pools、
创建权限或证明全局入网/HA。`managed_use_authorized=false` 和
`native_execution_attested=false` 始终保留。

## 代理设备：来源、连接和控制权必须分开

代理 capability 的 owner 配置绑定 `source_device` 与 `proxy_binding`。
挑战附 `proxy={source_device,proxy_binding,revision,ttl_seconds}`；副本/
迁移继续携带真实来源，不能为同一设备复制出更多性能或授权。
有 TTL 只表示声明期限，观察不等于使用，持有 lease 不等于已控制设备。

代理 `verify` 额外要求可信 `connection_observer(request)` **实际执行**
原来源/绑定/版本的连接挑战，返回
`{source_device,proxy_binding,revision,checked_at,expires_at,
method:'actual_connection_challenge',evidence_sha256}`。观察者由可信外层
配置，不能由 node JSON 假造；参考只核对回执绑定/时效，不实现 SSH/
NAS/手机驱动。陈旧 revision、未来采样、过期连接、ping-only 被拒绝；
登记期限不长于真实连接证据期限。登记没有设备控制权或升级权限副作用。

## 失败、续接与本片验收边界

callback/登记各先持久化 intent，再最多尝试一次。异常、进程崩溃、丢
ACK 都保留原 ID 和 unknown/attempted，不因重新入网、换模型、改版本
或查不到回执重做。原 late result 可补回同 nonce/内容；登记用
`reconcile_registration(operation_id, observer)` 只查原 publication，
缺回执不触发重发。期限到后只允许读回/核对原成果，不继续新调用。

本片测试是独立本地临时目录、真实 owned Python 子进程返回完整
153600 B 产物、独立 loopback TCP 两次挑战，以及 ACK 丢失/冷恢复/
并发/来源版本/TTL/权限边界。没有新模型、生产注册、真实第三台机器、
Windows/Harmony、owner NAS/SSH-only 设备或官方 Cloud 的实际运行证据。
后续由 Mesh 按当前实际可用设备做一个异构闭环；不预建全平台框架、
共识/CRDT、无限适配器或以本地 fixture 冒充真实入网。
