# 手机接线前只读交接（2026-10-31）

本片仅查看实际服务、精确 TLS 配置和既有源码，不改接收器、账号、签名、
私有配置或业务状态。OAuth bundle 原 WIP 留在其独立分支，未继续扩展。
原商业 unsigned HAP、API26/arm64 构建、调试证书与 CSR 配对证据继续复用，
没有重新构建、检查 Profile/密钥、读取浏览器材料或调用模型。

## 本轮实际确认

- VPS 用户 `assistant-mesh.service` active/running，入口仍为已部署的
  `amita-native-partial-receiver-20261030.1`。私有 API 17680 与 phone 17684
  由同一个接收器进程监听，均为 loopback；没有另建 Store/receiver。
- `assistant-mesh-cloud-node.service`、`amita-cloud-supervisor.service` 和
  系统 `personal-assistant-mesh-ip-tls.service` 均 active/running。
- 实际 Nginx phone allowlist 只有 `/enrollment/node`、`/phone/node`、
  `/v1/harmony/updates/manifest` 和精确 64 位小写 hash artifact 路径，均转到
  127.0.0.1:17684，保留 Host、固定 HTTPS、关闭上游自动重试。owner 管理接口
  未进入公网 allowlist；其余路径仍 404。
- 实际旧 ready 链构造带 public trust 的 UpdateService，再挂同一原 API 的
  phone listener。私有 data-release 下 manifest.json 存在，1304 bytes；
  本轮未读取其正文、服务 token 或签名密钥，也未把“文件存在”冒充下载/签名验收。
- 电脑 `amita-hdc.service` enabled，实际 listener 仅 loopback 8710。未执行
  HDC 设备动作、开放校园无线调试、操纵桌面或改 USB 状态。
- 本轮所查 phone-v1 目录中没有 owner binding 目录。没有创建 node、binding、
  enrollment、challenge、nonce 或任何假手机结果。

这些是服务与配置准备证据，不表示 Profile 已创建、HAP 已签名安装、真手机
已经入网，或后台回调已发生。签名/Profile 当前私有状态仍由 Root 独占确认。

## configuration-only plan 的精确状态

源码确认，无 enrollment ID 的 `phone_owner ... plan` 只发送
`POST /v1/phone/owner/plan {}`，返回 configuration_only、challenge_created=false
及已加载身份/operation 观察，不初始化任何 challenge。现有 10 月 29 日
phone-owner-live 已真实通过的证据仍保留。

本轮准备使用当前 receiver namespace 和已存在的 cloud operator 配置再调用
一次，但 Click 在执行前拒绝 SSH/env/module argv：
`check 1 is neither read-only nor a recognized check`。故本轮没有发送这次
plan 请求，没有不确定响应需要重试，也未绕过或新建验证框架。已把确切 CLI
路径参数交 Root，后续可沿原已支持的私有 owner CLI 验证入口完成一次；
不要把这次工具分类拒绝记录成服务调用失败或新鲜 plan 成功。

## 三种身份与任务不能混用

Huawei UDID 用于设备 Profile。应用 node_id 则由 NativeServices 首次
initialize 在应用私有状态中生成 `harmony-UUID`，后续读原 snapshot 保留。
不能提前用 UDID、硬件序列号或随机 owner 占位代替应用首次启动的真实 ID。

LiveTaskGate 在原 Store 事务内验证实际绑定的 task/epoch，要求原 task 仍
running、原 node/租约有效，并由 Store 执行相应 Leader 代际校验。随后必须
匹配原 ingress request_id；没有 ingress 行时才按原 task context 中的
request_id/origin message_id 观察。owner 操作还要求 session_scope 精确为
`leader:project:amita-harmony-onboarding`。

因此，已经 completed 的 phone-owner-tools **开发任务**不能再次当作真实入网
任务；正在进行的 S2 task/epoch 也不属于这个 scope。保留它们的完成结果、
原 unknown/before、原 thread 和全部收据，不恢复或改写来通过入网 gate。

本轮只读许可内没有获得一个已确认的真实 enrollment task ID/current epoch。
首启 ID 到达后，Root 必须在原 authority 沿实际手机入网请求选择/领取其原
task，读取当时有效的 epoch 和原 request ID，再写 settings；不能引用旧文档
中的开发 epoch、借用 S2 epoch2 或提前编造数字。

## 最短接线顺序

1. Root 复用原私钥、CSR、已配对调试证书和已核实的商业 unsigned HAP。
   若 Profile/同签名 HAP 仍未完成，使用既有私存真实 UDID 办理；不必为了重取
   一个已保存 UDID 重做构建，也不在本片检查账号、Profile 或密钥。
2. 合法签名安装并首次打开应用，取得并保留应用真实 node_id。此时尚未登记。
3. Root 确认真实 enrollment task 的 live node/epoch/request/scope，按
   [owner 操作契约](PHONE-OWNER-OPERATIONS.md) 放置固定 binding index、settings
   和原 Bootstrap 文件。ID 到达以前仅保留模板，不创建真实 challenge。
4. Root 用同一 enrollment ID 执行 `prepare-existing-binding`，再经获准初始
   设置通道交付 relay-export。手机主动调用原 `/enrollment/node` 并产生完整
   真实字节结果。丢 ACK 只查回原 operation，不换 ID/nonce 重发。
5. 结果实际到达后 Root 才执行 `verify` → `register`/原 Registry 独立观察 →
   `issue`。只向手机交付 issued 的 bootstrap；operator 和维护凭据不下放。
6. 原调用方下发第二个独立能力请求并读完整结果，然后验收本人选文件、已签名
   数据更新、拔 USB/断网重连。WorkScheduler 注册与真实后台 callback 分别记录，
   无 callback 就保留待验收，不宣称永久后台常驻或 HA。

具体剩余依赖是 Root 的 Profile/签名安装状态、真手机首启 ID，以及原 authority
的实际入网 task/epoch/request 绑定。服务侧只读准备已具备上述证据；不能靠
服务在线、已有 SDK 构建或原开发任务 completed 跳过这些真实步骤。
