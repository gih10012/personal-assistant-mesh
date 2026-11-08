# API26 公开无障碍兼容探测

`PhoneAccessibilityProbe.ets` 只使用真实商业 API26 SDK 仍公开导出的
`AccessibilityExtensionAbility.onConnect`、`AccessibilityExtensionContext.getWindowRootElement`
和 `AccessibilityElement.attributeValue`。它们标记 `@deprecated since 12`，
没有 `@systemapi` 或 `@permission` 注解；弃用本身不证明不可调用。
当前商业 SDK 的 module schema 接受 `type: accessibility`，华为当前
[module 配置 type 表](https://developer.huawei.com/consumer/cn/doc/HarmonyOS-Guides/module-configuration-file/)
也没有对这一类型写三方禁用（对 service/dataShare 等有明确禁用说明）。

但 SDK 同时存在新权限 `ohos.permission.ACCESSIBILITY_EXTENSION_ABILITY`：
`PermissionDefinitions.json` 标记 `system_grant` / `system_basic` /
`availableType: SYSTEM` / `provisionEnable: true` / `since: 20`。
这不等于旧公开 API 也必然要求该权限。探测不请求此系统权限、不申请 ACL，
不调用新系统私有回调，不伪造权限，也不使用 UiTest/HDC 作为应用控制后端。
旧公开链路是否仍可由用户设置启用并成功回调、取 root，留给真实设备验证。

Root 可在统一商业构建中把下面的组件声明增量加入现有 module 的
`extensionAbilities`，保留现有所有声明；本专员没有改 manifest：

```json
{
  "name": "PhoneAccessibilityProbe",
  "srcEntry": "./ets/control/PhoneAccessibilityProbe.ets",
  "type": "accessibility",
  "label": "$string:app_name",
  "description": "$string:module_desc",
  "exported": true
}
```

这里只给出 schema 已证实的最小组件声明。若当前系统设置需要附加的
accessibility metadata/profile 才列出服务，需按本版本真实 schema/文档
增加，不凭旧版样例猜字段。用户需自行在系统辅助功能设置中启用；代码不
设置开关、不请求全局写配置权限。没有出现服务/没有回调也是待定位结果，
不能写成“系统不支持”。

onConnect 首先持久写入 `connected_probe_pending`，随后只读获取当前 root，
只保存 root 可读与是否本应用这两个布尔。仅当 bundle 为本应用才尝试取
root 的 componentType，仍只保存可读布尔，不保存任何应用的包名、文本或
节点树。结果位于应用私有 Preferences `amita_control_probe_v1` 的 `status`
键，也可由 `readAccessibilityProbe(context)` 读取。错误只保留固定白名单
数字码，未知码归零。所有物理按键原样传递，动作能力始终 `false`。

`root_readable=true` 仅证明这次用户已启用服务可读 root，不代表已验证
跨应用树、任意动作、连续截图、全部物理输入观察或后台常驻。进一步开放
控制须逐项实测相应公开能力、获得本人的本地授权，并通过 ControlSession
的单次审批、当前对象与原 operation journal；这个 probe 自身没有动作入口。
