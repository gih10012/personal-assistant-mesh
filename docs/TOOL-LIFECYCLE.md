# 本地工具发布与回滚

`tool-release` 是可选的**本地原生 Shell 命令**。已经获准操作本机的
agent 可以自行编写、测试工具，再保存版本化的受管 Mesh 能力合同。
它不是远端源码安装接口，也不是原生 Shell、MCP、文件或联网白名单；
未登记的原生工具仍可使用。`kind`、描述、业务 `spec` 和 `action` 开放，
没有固定工具类型表。每个 candidate 的一个 adapter 是一次发布单元，
不是对其他工具或功能的禁止。

本控制器不运行测试、不执行模块、不安装依赖、不启用或重启服务，也
不改变 Leader 的会话。`stage` 只读本地私有输入并校验/编译 UTF-8
源码；不会执行顶层代码、导入或入口函数。真正执行继续使用
[provider 运行器和 admission 合同](MANAGED-ALLOCATIONS.md)。

## 私有输入与目录

下面 `/PRIVATE/...` 均是需要替换的绝对路径，不是现成部署配置。
输入文件必须本人所有、普通文件、`0600`、无 symlink/hardlink；直接
父目录必须本人所有且 `0700`，祖先不能是可被其他用户改写的不安全
目录。`--state-dir` 必须是**已经存在**的本人 `0700` 目录。凭据保留在
私有文件，不能放进 argv、描述、测试报告、公仓或 stdout。

客户端配置沿用现有 Mesh `Client`，例如：

```json
{
  "control_url": "http://127.0.0.1:17680",
  "token_file": "/PRIVATE/provider.token"
}
```

使用实际 provider 的认证 worker 身份，不借用 operator 凭据冒充节点。
`publish` 与 `activate` 会核对认证 `/v1/mesh/hello` 的 node/authority；
它们必须与 manifest 的 `authority_id` 一致。所有 CLI 动作会读取客户端
私有配置；`stage` 和 `prepare-rollback` 不发 authority RPC，脱网也能
保存候选版本。

现有 owner runtime manifest 可以先是空 adapters：

```json
{
  "schema": 1,
  "provider": "node:owned-node",
  "authority_id": "owned-authority",
  "journal": "/PRIVATE/provider.sqlite",
  "adapters": []
}
```

candidate 使用相同 schema、provider、authority_id 和**同一个** effects
journal，只包含待发布的一个 adapter：

```json
{
  "schema": 1,
  "provider": "node:owned-node",
  "authority_id": "owned-authority",
  "journal": "/PRIVATE/provider.sqlite",
  "adapters": [
    {
      "capability_id": "owner.storage.analysis",
      "handle": "owner.storage.analysis",
      "version": "1.0.0",
      "module": "/PRIVATE/storage_analysis.py",
      "sha256": "REPLACE_WITH_ACTUAL_64_LOWERCASE_HEX_SHA256",
      "function": "invoke",
      "capability_epoch": 1,
      "action": "storage.analyze",
      "new_spend_minor": 0
    }
  ]
}
```

SHA 占位符必须替换成实际文件哈希。`function` 必须是模块顶层唯一的
同名同步 Python 函数。`capability_epoch` 是本次希望发布的实际正整数
revision：初次是 1，更新必须预期当前 revision 正好是它减 1。
版本字符串、源码、函数、action 或安装位置改变，不能冒充旧 revision。
运行器可选的 `poll_interval_seconds`、`dispatch_limit`、`reconcile_limit`
仍沿用原 schema；激活保留当前 owner manifest 的其他设置和 adapters。

另一份独立 publication JSON 必须恰好包含四个字段：

```json
{
  "kind": "owner.storage.analysis",
  "description": "本机自主编写并测试的存储分析工具",
  "spec": {
    "result_format": "storage-analysis/1"
  },
  "test_reference": {
    "path": "/PRIVATE/storage-analysis-test-report.json",
    "sha256": "REPLACE_WITH_ACTUAL_TEST_REPORT_64_LOWERCASE_HEX_SHA256"
  }
}
```

测试报告是此前实际运行测试留下的私有字节；控制器只验证其哈希并
保留快照，不验证报告内容真实性，也不会重跑测试。输出始终标记
`self_tested`，不是独立验证、性能实测或实际远端执行证据。`spec` 不得
自行提供保留字段 `managed_adapter`、`tool_test`；控制器生成它们。
完整广告 metadata 若会被 registry 的敏感字段/URL 清洗改变，则拒绝
发布；自由文本仍须由作者避免放入秘密，清洗不是完美秘密检测。

owner/candidate manifest 最大 65536 bytes、owner adapters 最多 256；
单模块和测试报告各最多 1 MiB。完整 capability spec 包括生成字段受
registry 的 32768-byte 上限约束。

## 显式分阶段操作

从项目根目录使用现有 Python 环境执行，operation ID 在一次部署及
其恢复期间保持不变：

```sh
python3 -m assistant_mesh --config /PRIVATE/provider-client.json tool-release --owner-config /PRIVATE/provider-owner.json --state-dir /PRIVATE/tool-releases --operation-id storage-v1 --action stage --candidate-config /PRIVATE/storage-candidate.json --publication-config /PRIVATE/storage-publication.json

python3 -m assistant_mesh --config /PRIVATE/provider-client.json tool-release --owner-config /PRIVATE/provider-owner.json --state-dir /PRIVATE/tool-releases --operation-id storage-v1 --action inspect

python3 -m assistant_mesh --config /PRIVATE/provider-client.json tool-release --owner-config /PRIVATE/provider-owner.json --state-dir /PRIVATE/tool-releases --operation-id storage-v1 --action publish

python3 -m assistant_mesh --config /PRIVATE/provider-client.json tool-release --owner-config /PRIVATE/provider-owner.json --state-dir /PRIVATE/tool-releases --operation-id storage-v1 --action activate
```

`stage` 在 fresh 私有 release 目录中保存源码、测试报告、candidate
快照和安装 descriptor。descriptor 包含 handle、version、源码 SHA、
function、action、零新支出，以及**新安装位置的哈希**
`module_path_sha256`；目录对外只广告位置哈希，不广告源码或私有路径。
release 是按内容校验的保留快照，并非可抵抗恶意同 UID 进程的文件系统
不可变存储。相同 operation ID/输入是幂等读取；改变输入则冲突。

`publish` 先核对 authority 身份和 actual predecessor，再将 publication
intent 写入独立 `lifecycle.sqlite`，然后至多发起一次 `advertise` POST。
广告租约当前为 86400 秒；控制器不自动续租，长期使用须另行维护实际
能力租约。成功还需 authenticated read-back 核对 provider、exact epoch、
kind、description 和完整 spec。目录与广告只是声明，不证明执行。

响应丢失、超时、崩溃或不匹配会保留 `unknown`，CLI 返回 exit 2。
再次对**同一个 operation ID** 调用 `publish` 只查询原期望 revision；
即使查询认为尚不存在，也不再次 POST。若曾只落盘 intent、尚未发出
请求就崩溃，同样保守查询。不能用新 ID、epoch、清库或重建目录绕过
未知状态。确认权威 exact 原 revision 后才变成 `published`；不同或更
新的 revision 不能当成原操作的确认。超过 1000 条的满页能力目录不能
作为初次能力不存在的证明，会报告目录不完整。

`activate` 是另一次显式动作：再次核对广告，再保存 owner-before /
owner-after 私有文件和 activation intent，原子替换 owner manifest。
同一 owner manifest 的不同 state-dir 控制器共享 owner lock，并在替换
前比较 exact 原字节和身份；已观察到的其他配置修改不被覆盖。超过
manifest bytes/adapters 上限不会替换原配置。中途失败保留原状态，
重启后仍用原 operation ID 核对；不要删除备份或 lifecycle journal。
这是合作进程的协调，不是对故意忽略锁的恶意同 UID race 的安全边界。

## 状态不等于正在运行

`state` 是持久部署历史，包含 `staged`、`published`、`unknown`、
`activation_intent`、`manifest_activated`。旧版本被新版本替代后，旧
operation 的历史 state 不会伪造回退：

| 输出字段 | 实际含义 |
| --- | --- |
| `activation_recorded` | 这次部署曾记录 manifest 激活 |
| `manifest_binding_current` | 当前私有 owner manifest 仍包含 exact 这次 adapter |
| `manifest_activated` | 激活记录存在，且本次 binding 仍是当前配置 |
| `runtime_reload_required` | 当前配置这次 binding 需要另行让运行器采用；不证明已 reload |
| `runtime_loaded_verified` | 始终 false，控制器不观察或操纵服务 |
| `execution_verified` / `performance_verified` | 始终 false，不把安装或广告当成实际执行/性能证明 |

`inspect` 是本地读取，不确认当前 authority 或运行进程。实际 reload、
正在采用哪一版本、真实调用和静止，须另外验收。普通 Node/Worker 不会
因此自动启用 provider，也不会停止其原生工作或心跳。

## 回滚是新部署，不是倒退账本

例如当前实际 revision 为 2，准备恢复保留的 `storage-v1` 字节时：

```sh
python3 -m assistant_mesh --config /PRIVATE/provider-client.json tool-release --owner-config /PRIVATE/provider-owner.json --state-dir /PRIVATE/tool-releases --operation-id storage-rollback-v3 --action prepare-rollback --retained-operation-id storage-v1 --epoch 3

python3 -m assistant_mesh --config /PRIVATE/provider-client.json tool-release --owner-config /PRIVATE/provider-owner.json --state-dir /PRIVATE/tool-releases --operation-id storage-rollback-v3 --action publish

python3 -m assistant_mesh --config /PRIVATE/provider-client.json tool-release --owner-config /PRIVATE/provider-owner.json --state-dir /PRIVATE/tool-releases --operation-id storage-rollback-v3 --action activate
```

`--epoch 3` 必须按实际权威 revision 选择，不能照抄示例。回滚 staging
仍脱网：复制保留的旧源码/测试报告到新 release 位置，产生新 location
hash，并要求新 epoch 大于保留部署的 epoch。`publish` 再核对 actual
predecessor；旧代码的版本字符串可保留，但权威 revision 不递减。
这是一次已明确决定的新部署，不是为了绕过未知旧操作而更换 ID。

provider/authority/effects journal 身份保持不变。本控制器从不打开、
修改或重置实际 `provider.sqlite`，也禁止把它放在 lifecycle state
子树或与控制文件别名重合。已有 unknown invocation 不重放、不虚报
释放占用；已有真实 result 继续由 provider 按原 ID 补结算。

## 仍需独立完成的边界

这条链路保留一个 Python 模块，不打包/固定所有依赖，不是供应链沙盒、
CPU 强制配额或零费用效果证明。`new_spend_minor=0` 是受管合同约束，
不能阻止受信任原生程序自行调用其他接口；仍须遵守本人授权与预算。

发布工具不自动创建共享容量或远端许可。正式 managed admission 仍需
owner 的 pool/binding、跨主体 exact action/scope grant 和当前独立
观测；self-test 报告不能代替它们。owner 预授权的自主绑定/授予委托
属于后续功能，不能用 operator 全权限凭据给所有模型来替代。

单元测试和隔离 HTTP fixture 只证明实现覆盖的合同。完整实际验收还要
证明：原生 agent 自行编写/测试工具，安装发布后发生真实 admitted
调用、独立核验结果，再完成升级/失效恢复/新 epoch 回滚。本文不以
fixture 或正在进行的验收宣称模型自主工具生命周期、自治授权或整个
personal-assistant-mesh goal 已完成。
