# 近距离蓝牙键鼠

`assistant_mesh.paired_phone_input` 是电脑侧可选工具。它通过 BlueZ 的 BLE HID Report 协议发送键盘、相对鼠标和滚轮报告。设备范围为本人明确选定的现有配对手机。手机是否会在 HarmonyOS 零售系统中发现该 HID 服务、订阅报告并处理输入，仍需真机验收。

系统 Python 需安装 `dbus-python`、`PyGObject`，BlueZ 需提供 `Device1.Bonded`、`GattManager1`、服务端 `AcquireNotify` 和加密认证特征标志。需要现有的 LE 绑定，且该链路能满足 BlueZ 的认证加密要求。若系统设置使用 BR/EDR 配对、普通未认证的 LE 链路，或手机只支持从公开广播发现新的键鼠，本工具会在发出输入前停止。报告模式实现覆盖所列输入。当前未做完整 HOGP 配置和一致性认证，包括 Boot Protocol、Battery Service 和 Scan Parameters Service。

## 绑定与执行

本人通过系统蓝牙界面建立并确认配对。在电脑准备一个现有的 `0700` 状态目录。绑定和请求各为本人拥有的私有 JSON 文件，权限 `0600`。固定绑定中填实际手机节点、BlueZ 适配器和系统配对中的设备身份地址。请保留这个状态目录，用同一个目录查询原操作回执。

```json
{
  "format": "amita-paired-phone-input-binding/1",
  "phone_node_id": "ORIGINAL_PHONE_NODE",
  "adapter": "hci0",
  "peer_address": "00:11:22:33:44:55",
  "report_modes": ["keyboard", "pointer"],
  "state_dir": "/PRIVATE/paired-phone-input"
}
```

每个请求提交原 Mesh 任务、任务期次、手机节点和专用于该次蓝牙输入的原操作 ID。保留该 ID 查询回执。不要把既有手机控制操作的待核对结果改由蓝牙重做。此电脑侧 CLI 的终端确认是本次输入许可，任务身份字段用于关联，CLI 未接 Mesh 远端调度或手机本机审批。

```json
{
  "format": "amita-paired-phone-input/1",
  "phone_node_id": "ORIGINAL_PHONE_NODE",
  "task_id": "ORIGINAL_TASK_ID",
  "task_epoch": 1,
  "operation_id": "ORIGINAL_BLUETOOTH_OPERATION_ID",
  "action": {"kind": "move", "dx": 16, "dy": 0, "wheel": 0}
}
```

```sh
python3 -m assistant_mesh.paired_phone_input --binding /PRIVATE/binding.json --request /PRIVATE/request.json prepare
python3 -m assistant_mesh.paired_phone_input --binding /PRIVATE/binding.json --request /PRIVATE/request.json run
python3 -m assistant_mesh.paired_phone_input --binding /PRIVATE/binding.json --request /PRIVATE/request.json status
```

`prepare` 只校验绑定和输入格式。`run` 在前台终端显示手机和完整动作，收到 `OPEN` 与显示的摘要后开放最多 30 秒。默认要求现有连接，不注册广播。已配对但未连接时，可由本人明确运行 `run --advertise`，这会在非配对状态的适配器上注册限时、非可发现的 HID 服务广播。BlueZ 的公开广告 API 无目标设备字段，因此附近设备仍可看到广播中的通用 HID 服务 UUID。广播无输入内容或自定义身份。CLI 不调用配对、信任、连接或修改适配器设置。手机须从系统蓝牙界面连接原配对并订阅报告，也可接受系统正常弹出的安全确认。

BlueZ 回调必须来自当前系统总线上的蓝牙守护进程，并带绑定手机的 `Device1` 路径和 `LE` 链路标记。工具等待认证加密特征请求、属该连接的通知套接字，再请本人目视手机当前界面并确认 `SEND`。每次会话只处理这一项操作。断开、解绑、蓝牙守护进程更换、终端取消和过期会结束会话。GATT 服务注册在适配器级。报告只通过目标连接的套接字传给 BlueZ。

| 动作 | 参数 |
| --- | --- |
| `key` | `key` 为小写字母、数字、`enter`、`escape`、`backspace`、`tab`、`space`、`home`、`page_up`、`delete`、`end`、`page_down`、`right`、`left`、`down` 或 `up`，`modifiers` 为 `ctrl`、`shift`、`alt`、`meta` 中的无重复数组 |
| `text` | `text` 为 1 至 32 个可打印 ASCII 字符，按美式物理键盘布局发送。手机上的布局或输入法会影响结果 |
| `move` | `dx`、`dy`、`wheel` 各为 -127 至 127 的相对量，至少一个非零 |
| `click` | `button` 为 `left`、`right` 或 `middle`。在手机当前光标位置执行 |

发送前会持久记录原操作。`reports_queued_to_bluez` 仅表示报告写入 BlueZ 的本地套接字，`phone_effect_verified` 始终为 `false`。中断后的 `dispatch_unknown` 保留原操作，重复运行只查询该回执。对照手机画面确认实际响应。此工具提供近距离物理键鼠输入。屏幕读取、语义元素、绝对坐标、面向远程任务的原生系统授权及手机侧人工输入礼让由原手机控制路线负责。

实现依据可参阅 [Bluetooth SIG HOGP](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/HOGP_v1.0/out/en/index-en.html)、[BlueZ GATT Characteristic API](https://github.com/bluez/bluez/blob/5.87/doc/org.bluez.GattCharacteristic.rst)、[BlueZ LE Advertisement API](https://github.com/bluez/bluez/blob/5.87/doc/org.bluez.LEAdvertisement.rst) 和 [BlueZ 5.87 GATT server](https://github.com/bluez/bluez/blob/5.87/src/gatt-database.c)。
