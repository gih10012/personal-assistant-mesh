# 手机设备配置导入

首页“导入设备配置”先读取本应用 `context.filesDir` 下固定的
`amita-owner-setup.json`。只有此文件不存在时才打开系统 DocumentViewPicker。
配置必须是完整 UTF-8 JSON 文件，大小为 1–65536 字节；不接受配置中指定的
路径、URL、命令或额外字段。调试暂存由已获授权的本机调试工具放入应用自己的
私有目录；不要把 token 放进 shell 参数、日志或公共仓库。

暂存文件一旦读取消费，无论解析成功或失败都会尝试删除；删除失败也报导入
未确认，不继续绑定。系统选择的用户原文件不会被删除。选择配置的临时 URI
不保存、不进入 `file.read.selected`，也不授予共享能力。

封套严格包含以下八个字段，所有字段必须出现，未使用的字符串显式为 `""`：

```json
{
  "format": "amita-phone-owner-setup/1",
  "node_id": "当前应用首启产生并持久保存的节点ID",
  "phase": "enrollment",
  "enrollment_json": "原 mesh-enrollment/1 的 JSON 字符串",
  "relay_json": "原 mesh-enrollment-relay-client/1 的 JSON 字符串",
  "relay_token": "可信服务签发的单 operation scoped token",
  "work_bootstrap_json": "",
  "update_trust_json": ""
}
```

`node_id` 必须逐字匹配本应用持久身份。`enrollment` 阶段要求 enrollment、
relay 和 token 非空，调用原 `bindEnrollment`、`bindRelay`。绑定本身不发起入网
请求，不领取/执行挑战，不自行注册，不开启共享；已配置的原成员客户端仍按
原机制维护连接与核对回执。relay token 不回显文本框、
不保存 Preferences，只在本次前台私有内存中供下一次明确点击的 relay 动作使用；
该动作无论成功失败均清除。离开前台、再次导入或编辑 token/relay/operation
也会清除，后续查询可重新导入原配置。本人仍需单独打开共享并点击原动作。

原登记已独立核验后，用第二份文件导入 `member` 阶段：

```json
{
  "format": "amita-phone-owner-setup/1",
  "node_id": "同一持久节点ID",
  "phase": "member",
  "enrollment_json": "",
  "relay_json": "",
  "relay_token": "",
  "work_bootstrap_json": "原 mesh-phone-bootstrap/1 的 JSON 字符串",
  "update_trust_json": ""
}
```

`member` 要求前三个协议字段为空、bootstrap 非空。原 `WorkClient.bindBootstrap`
仍验证当前节点、原 enrollment、本机完整成果、registration receipt 和原 session，
之后把节点凭据写入 Asset。原客户端可建立出站连接并核对原回执；共享保持关闭，
导入不领取或执行新任务。实际连接、任务和 unknown 继续显示在原状态栏，
“配置已导入”不代表连接成功。

`update_trust_json` 可为空；非空时必须是匹配本节点且 `enabled=false` 的原
`amita-update-trust/1` JSON 字符串，继续调用原可信维护校验与存储。导入不会
启用后台同步；启用仍通过独立的维护开关。enrollment 阶段不接受维护配置。

导入成功、失败或取消都会清空 UI 中 enrollment、relay、bootstrap、维护配置、
token 和手动挑战原文。错误只显示固定说明，不包含输入 JSON/token。
多步绑定沿用各原服务的耐久提交；后一步失败不会撤销已成功的原绑定，UI 明示
“未全部确认”，应使用相同原配置核对，不换节点或 operation 重做。

必要的同源协议检查（在 Click Evidence 内运行）：

```sh
click-gate verify -- node --test apps/harmonyos/test/setup.test.mjs
```

真实商业 SDK 构建、同签名保留数据更新，以及私有目录/系统选择器真机验收
由串行手机运维完成；Node 协议检查不代表这些步骤已完成。
