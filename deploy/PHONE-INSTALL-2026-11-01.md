# 本次鸿蒙接线操作单

面向当前已获授权的本人真机窗口；通用接入契约仍见
[owner操作](PHONE-OWNER-OPERATIONS.md)。设备、签名材料、口令和真实业务资料
只保存在私有状态，以下没有真实设备标识或 token。

## 已备好

- 原商业API26/arm64构建，原 AMITA 应用与 Huawei 调试证书。
- 真实 UDID 已登记，单设备 Profile 生效至2027-10-29。
- 原密钥官方工具签名成功的 HAP，430606字节，私存
  `~/.local/state/amita-native-agents/phone-signing-20261028/private/`。
- 该目录 `signing-result-20261101.json` 记录精确包名、产物、hash和签名结果。
  口令通过交互输入，未进入命令参数。独立验签命令被 Click 分类审核拒绝，
  未执行、未冒称通过；手机安装系统校验与实际入网验收仍待进行。
- 原 receiver、phone listener、精确公网 TLS 路由及电脑 loopback HDC。

## 本人最少操作

使用可传数据的 USB 线连接电脑，解锁手机并允许本机 USB 调试授权。
无需重做华为账号认证或开启校园无线调试。待系统安装确认时按实际提示操作。
文件能力验收时选择一份非敏感小文件，其余命令与配置由 Root 执行。

## Root 安装窗口

1. 只从原 loopback HDC 服务读取当前目标，与私存真机记录对应；没有目标、
   未授权或身份不匹配就等待实际手机，不改为校园公网调试。
2. 使用私存签名 HAP 正常安装；保留原目标、安装回执和系统错误。安装失败
   不卸载清数据、不换证书或伪造 Profile，以实际错误定位。
3. 正常启动 `org.personalassistant.mesh / EntryAbility`，读取页面显示的实际
   应用 node_id。此 ID 由应用首次生成，不能替换成硬件 UDID、serial或占位值。

本机已部署的 connector 命令如下。`REAL_CONNECT_KEY` 必须来自本次目标观察，
只有本人手机接回后才代入运行；不要原样执行占位命令。

```sh
/home/gihggiy/.local/share/amita/tools/hdc-26.0.0/hdc -s 127.0.0.1:8710 list targets
/home/gihggiy/.local/share/amita/tools/hdc-26.0.0/hdc -s 127.0.0.1:8710 -t REAL_CONNECT_KEY install /home/gihggiy/.local/state/amita-native-agents/phone-signing-20261028/private/amita-phone-20261101-signed.hap
/home/gihggiy/.local/share/amita/tools/hdc-26.0.0/hdc -s 127.0.0.1:8710 -t REAL_CONNECT_KEY shell aa start -a EntryAbility -b org.personalassistant.mesh
```

## Root 入网窗口

沿原 authority 接收真实 onboarding 请求，读取其实际 live task/epoch/request，
scope 必须是 `leader:project:amita-harmony-onboarding`。唯一现存 completed
工具开发任务不是本次真实入网任务；不更改它、不借用 S2。实际可用的任务
准入/领取入口应在通知本人接线前备好，不编造 claim 的 task_id 过滤参数。

真实 node_id 到达后，按 owner契约放置私有 settings/Bootstrap，再沿同一
enrollment ID 执行 prepare-existing-binding。只把单操作 relay 配置/凭据交给
手机，手机主动完成原挑战并保存完整原结果；不提前创建挑战或冒充执行结果。
完成后 verify → register（独立Registry观察）→ issue。手机仅获取 issued
bootstrap，将节点凭据存入 Asset；不获得 operator 凭据。

第二个独立业务请求经原 Registry/Allocation 领取、完整结果与结算回到原调用方。
再做本人选文件、签名数据/skill更新、拔USB及网络重联。遇unknown保留原ID，
先查原回执，不重装清记录或换ID重做。真实 WorkScheduler callback 未发生时
继续标待验收；不把注册调度或一次前台同步叫作后台常驻成功。

完成初始调试后关闭手机无线 HDC。正常使用与应用数据更新经手机主动发起的
AMITA HTTPS，原生代码/权限更新仍用合法同签名 HAP和系统安装流程。
