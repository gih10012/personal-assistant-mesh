# 手机原生 enrollment relay 薄适配

本片在现原生 App 内增加自动 **原挑战领取 → 本地执行 → 完整结果首次回传**。
不是只靠手工复制结果，也不是另造 Node/Membership 协议。服务端复用
[原 Journal relay](../../docs/INTERACTIVE-ENROLLMENT-RELAY.md)，客户端仍用
同 `mesh-enrollment/1` / `challenge/1` / `result/1`、原 continuation 与
原本地 `JournalRecord`。仅 source + 同 TS 协议测试，**未声明真机网络通过**。

## 本人配置与操作

先导入本人已批准的原 enrollment（interactive，node_id 必须是 App 显示的
原身份）。再从可信 owner 安装流程取得下面的 **原任务** client config；
它不是 App 自己创建或批准的任务，也不含 token：

```json
{
  "format": "mesh-enrollment-relay-client/1",
  "resource_url": "https://mesh.example.test/enrollment/node",
  "binding": {
    "mesh_id": "original-mesh",
    "node_id": "original-phone-node",
    "owner_binding": "original-owner",
    "enrollment_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    "operation_id": "original-operation",
    "request_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    "nonce": "0000000000000000000000000000000000000000000000000000000000000000",
    "expires_at": 2100000000,
    "task_id": "original-task",
    "task_epoch": 1,
    "entry_request_id": "original-owner-request"
  }
}
```

以上全是占位，不能直接用于入网。binding 必须逐字段等于服务端可信 owner
配置与原 challenge。client config 严格拒绝 token/密码等额外字段。重复
导入同 op 不清除旧 attempted/unknown，也不能改 URL、任务 epoch、nonce。

页面上依次绑定原远程任务、输入单 operation 的 scoped token、开启本次
能力共享，点击“远程领取 → 执行一次 → 完整回传”。若是选定文件能力，
先用原系统 FilePicker 选择一个文件，再允许本次 Mesh 使用。保留手工
原结果查回，无需重新选文件或执行来补回结果。

token 只存在当前页面/调用内存，每个动作完成、失败或进入后台后清空页面
字段；不进入 Preferences、journal、成果、日志或公开仓库。它与旧 peer
hello token 不混用。本片没有获取或发行任何真实凭据。

## 固定身份与两个阻重边界

`NodeSnapshot.relays` 只是原操作的传输检查点，存在同一个应用私有 snapshot
内，最多64条，不含 token/URI，不是新的成员或授权数据库。旧版无此字段
时只补空数组；原 native 执行记录/完整结果/unknown 保留，不创建替代身份。

1. **领取前**：同 op 的 challenge 网络意图先持久化 `attempted`，然后才
   POST `/enrollment/node`。收到严格布尔 `may_execute_original_challenge === true`
   且完整原 binding、challenge hash/nonce/continuation/期限全部匹配，才进入
   既有 `beginOperation`，持久化本地原 execution `attempted` 后才触碰文件/执行。
   `false` 只保留观察。丢 ACK、崩溃或只收到 grant 后重启，均转 `unknown`；
   再点远程按钮只查原 `status`，不重新领取或执行。手工粘贴同 op 也不能
   绕过该管理阻重，但无关的新原任务与手机其他原生功能不受限制。

2. **结果提交前**：保留原完整结果，对 canonical base64 的全部 byte count/
   SHA256 与原绑定重新核对；原 result envelope 的 canonical ASCII SHA
   与服务端 Python 完全相同。首次 POST 前再持久化传输 `attempted` 与完整
   envelope SHA。POST 结果未知后，任何按钮都只查原 `status`，**不再 POST
   同一结果、换 ID 或换 endpoint**。只有远端原 `result_sha256` 与本地完整
   envelope SHA 一致，才记 `acknowledged`，不是把 `provider_reported` 当
   “还没执行”，也不是把回执当独立执行证明。冲突保持 unknown。

若在领取响应丢失前其实尚未执行，或者结果 POST 尚未到服务器就断网，
本片宁可保留未知，也不猜测“没发生”后自动重复。原完整本地 result 永远
仍可用原 operation ID 查回；后续可信人工/owner reconciliation 可在原
身份上处理，不能借此创建一个“新任务”重放。

## 网络、权限与真实限制

只请求 owner 已给出的 HTTPS443 **精确 `/enrollment/node`**，不走通用
`/v1`、不自动换公网或代理。API23+ 才允许携带 token，使用 `maxRedirects: 0`、
单次 `http.request`、无缓存、8秒连接/15秒读取与256KiB响应上限，保留系统
TLS/代理默认值，不关闭 CA 检查。当前薄 URL parser 支持域名/IPv4，不宣称
IPv6 原点兼容；不影响设备 agent 自己原生联网/修复能力。

[OpenHarmony 官方 HTTP 类型](https://github.com/openharmony/interface_sdk-js/blob/master/api/%40ohos.net.http.d.ts)
说明 POST 的 `extraData` 可承载请求体，API23 的 `maxRedirects=0` 可禁止
重定向；本片沿已用的兼容接口，不启用 API26 新 `body` 参数或改宿主代理。
每次 request 后 destroy 原 request 对象。256KiB 是传输边界，不提高原
64KiB完整成果预算；超出拒绝而非截断。Preferences 仍是单进程参考状态
持久化，非加密凭据库/跨进程数据库，长期大量完整成果的真机容量需实测。

前台/本人开关/临时文件 grant 仍是本地最终条件，后台或锁屏撤销共享；
异步下载后再次检查，不凭服务端 grant 绕过手机权限。只使用已有字节 recipe
或选定文件只读能力，不获得相机/全部文件/系统小艺/GUI 权限。仍未自动
verify/register/grant，亦未内置模型或承诺后台常驻。

服务端仍需要 Root 安装真实 live-task validator、可信单 op credential 和
**显式开启 TLS 路由**；当前正式 TLS allowlist 尚未开 `/enrollment/node`。
validator 是原任务权威的时点检查；其 TOCTOU 要由实际安装侧串行 eligibility
或当前 generation/lease 处理，不冒充跨数据库共识。服务端 first result
过期/执行禁用后拒绝新增写；客户端只保留原本地结果/原状态，不自动扩大
权限或重发。服务端允许已保存相同 bytes 的幂等回执，并不意味着本客户端
会在未知 POST 后重新上传。

## 本片验证

```sh
node --test apps/harmonyos/test/protocol.test.mjs apps/harmonyos/test/relay.test.mjs
python3 -m unittest tests.test_harmonyos_node tests.test_harmonyos_relay \
  tests.test_enrollment_relay tests.test_enrollment tests.test_enrollment_bootstrap tests.test_cloud_http
```

首片24个同源 Node cases（原12+新增relay12），后续SDK编译修正增加1个
共享状态读取用例，当前25项通过；Python另调用真实
`EnrollmentRelay` 原 intent/admission/result，交给同一 `Protocol.ts` 验证
完整 canonical binding/result/hash，另检查原生适配调用顺序。测试组有包含
关系，不相加冒充不同系统验收。它们不运行 NetworkKit、Preferences flush
或手机生命周期，不证明商业 Harmony SDK、真机TLS或完整 Mesh 入网。
固定83bb76e客户端与021e482配置的真实 public OpenHarmony 完整unsigned
构建已独立通过，含4处控制流类型错误的原失败及最小修正，详见
[SDK构建记录](BUILD-VALIDATION.md)。该新HAP包含客户端源码，但采用
public SDK/default设备overlay，不能拿旧HAP或新HAP冒称手机运行通过。
