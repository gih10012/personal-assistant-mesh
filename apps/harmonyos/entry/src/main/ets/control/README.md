# 手机操控、观察与审批接线

当前状态（2026-11-11）：本目录已由 Root 接入真实入口、协议与 manifest，
经过商业 SDK 构建并实际覆盖安装。系统能列出本辅助扩展，但没有取得启用、
onConnect、跨应用树或动作证据，尚不是可用的生产手机操控服务。
官方当前能力范围、GitHub 同类复现与后续路线见
[GUI 路线核实](../../../../../../../docs/HARMONY-GUI-ROUTES-2026-11-11.md)。
不再以“旧声明存在”作为继续寻找启用开关的理由；后续设备操作由 Root 串行
协调。下文保留11月8日接口调查与核心设计，后续实现状态不由旧快照推断。

## API26 已核事实（2026-11-08）

已读取真实商业 SDK 的 `sdk/default/openharmony` 和 `sdk/default/hms`，不是
用旧 OpenHarmony 私有接口或 SDK stub 代替商业手机 API。

| 能力 | 本机 SDK / 官方证据 | 本阶段处理 |
| --- | --- | --- |
| 无障碍 Extension | AccessibilityKit 仍导出 AccessibilityExtensionAbility；其 onConnect/onAccessibilityEvent/onKeyEvent 及 Context 的 root/windows/gesture、Element 的 attribute/action 均公开但标记 API12 起弃用，没有 systemapi/permission 标记 | 弃用不等于失效；独立 PhoneAccessibilityProbe 让用户系统设置启用后只读验证，详见 PROBE-INTEGRATION.md；动作不因声明存在自动开放 |
| 新无障碍权限 | PermissionDefinitions.json：ACCESSIBILITY_EXTENSION_ABILITY 为 system_basic/system_grant/SYSTEM，since20，可通过 provision 开启；QUERY_ACCESSIBILITY_ELEMENT 为 system_core/SYSTEM | 不申请系统权限、不调用私有新 API，不推断旧公开链路也必然要求它 |
| 无障碍组件声明 | 商业 module schema 包含 accessibility；官方 type 表没有像 service/dataShare 那样对其写三方禁用 | 先用合法最小声明探测安装/设置/回调/root；缺 metadata 时先定位，不能把未列出或没回调当“不支持” |
| UiTest | @ohos.UiTest 的对象标记 @test，syscap 是 SystemCapability.Test.UiTest | 不作生产后台控制后端，不依赖 USB/HDC |
| 选区截图 | ArkUI screenshot.pick()，用户交互式选择，返回 pickRect 与 PixelMap | SelectedScreenCapture：一次用户手势触发，有界缩放/编码，不保存/上传，不宣称连续观测 |
| 程序化全屏截图 | screenshot.capture() 真实存在；SDK 要求 CUSTOM_SCREEN_CAPTURE 或 CUSTOM_SCREEN_RECORDING；前者在 PermissionDefinitions 为 normal/user_grant/NORMAL，后者 system_basic/manual_settings 且设备限 2in1/tv | 没有因它需权限就宣称三方不可用；本阶段未接线声明/申请/真机调用，连续全屏截图尚未实现或验收，不能把 pick 当替代 |
| 华为系统 Agent GUI | 官方说明提供合作申请及 AI_Operation_Mode / Agent DID 查询，伙伴需按双方确认的范围接入系统底层能力 | 没有公开可直接替换成通用注入器的 API；未申请/未获准，不冒充系统 Agent |
| LiveView | HMS LiveViewKit 真实 start/update/getActive/isEnabled；本地展示和场景权益、调测设备是不同门槛；serviceButtons/clickAction 是 WantAgent | ApprovalSurface 真实调用；开关/权益/发布未确认时回退真实系统通知，仍保留原 pending；未开通知则明确 in_app_pending |
| 后台存活 | LiveView 展示生命周期不等于运行应用或保活；SDK AUTO_STOP_WHEN_APP_TERMINATE 可随进程结束移除本地窗 | 没有增加后台服务权限、无限循环或假唤醒；已有前台 WorkClient 的后台限制未改变 |

官方原文及出处：

- [无障碍 Extension 公开 API](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/js-apis-application-accessibilityextensionability/)
  与 [Context / Element](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/js-apis-inner-application-accessibilityextensioncontext/)
  标明 API9 起提供、API12 起弃用；这是弃用说明，不能外推真机拒绝。
- [module 配置文件](https://developer.huawei.com/consumer/cn/doc/HarmonyOS-Guides/module-configuration-file/)
  列出 accessibility 类型，并对部分其他类型明确说明三方限制。
- [系统 Agent GUI 机制](https://developer.huawei.com/consumer/cn/doc/service/agent-gui-0000002680521240/)
  说明伙伴合作接入、用户授权与可暂停机制；状态查询不提供操控权限。
- [LiveView API](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/liveview-liveviewmanager/)
  定义场景、生命周期、WantAgent、关联服务按钮、sequence 与返回码。
- [实况窗错误码](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/liveview-error-code/)
  的 1003500005 区分调测权益、设备添加、添加后 24 小时生效及正式场景权益。
- [正式权益申请](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/liveview-formal-authority/)
  说明 AppGallery Connect 中按场景申请与评审；代码不能自行选择一个不相符场景规避。
- [通知操作按钮](https://developer.huawei.com/consumer/en/doc/harmonyos-references/js-apis-inner-notification-notificationactionbutton/)
  定义真实 WantAgent 按钮；[用户授权](https://developer.huawei.com/consumer/cn/doc/HarmonyOS-Guides/request-user-authorization/)
  要求先声明权限，由真实用户动作申请并核对结果。

## 原任务、对象、屏幕与本人输入

ControlSession 使用原 task_id/operation_id，不新建业务任务。inspect 的
element_ref 绑定本次进程 session、窗口和 revision；换观察、窗口变化、锁屏、
断开或本人输入都会使旧 ref/审批失效。inspect 返回有界元素、明确 truncated，
密码节点标签脱敏且不能交给动作。没有坐标猜测或任意 shell 动作。

屏幕资源名称为 `screen:<node_id>:display-0`。主 authority 仍须按现有资源租约
授予唯一 provider；本地一进程一 ControlSession/ControlStore 又限制同一时刻
只有一份短租约与一次动作，最长 30 秒。这里没有声称单个 JS 对象能对不同进程
或不同服务做分布式锁。所有 UI/Extension 决定须路由到同一屏幕 owner，不能在
两个进程各开一个 executor。ControlStore 实际等待私有 Preferences flush；
旧 attempted 冷启动改为 unknown，未决审批标记 cancelled 保留记录并需重新观察。

获得系统权限、能读树、能观察所有实际人工输入、能注入动作是分别验证的能力。
ControlProvider 必须报告真实 availability，并在动作的最后 admission 检查
当前 bundle/window/view_token/实际元素/fence。人输入事件直接传给 ownerActivity，
立即撤销 fence/ref/短租约，让人继续用手机，不截获按键。旧 Accessibility 回调
中的 touchBegin 可能只对应触摸浏览模式，不能据此假设已看到所有实际触摸；
未证实 human_input_observable 时核心拒绝动作。

## 一次审批与不确定结果

1. 真实 provider 的只读观察通过后，用户在本地授权此控制 session；远端工具
   不能调用 setOwnerEnabled。acquire 绑定原 task，再 request 原 operation。
2. request 把完整固定请求、对象 revision、短租约、随机 approval_id/nonce 和
   截止写入私有 journal 后才返回票据。**票据和 nonce 只在本机传给审批 surface，
   不序列化给远端调用方、日志或公共仓库。** 外部只得到 operation 的 receipt。
3. ApprovalSurface 不改 journal。只选本应用已经获准且业务匹配的 LiveView event；
   没有权益配置时 event 为空，直接使用系统通知。发布失败不延长时间、不批准。
   两种 surface 都不可用则呈现 in_app_pending，调用方必须显示原票据的本地
   ApprovalUI，不能丢掉或自动批准。通知授权须由本人的 UI 手势另行申请。
4. buildApprovalWantAgents 构造固定本包 EntryAbility 的不可变、一次性 WantAgent，
   绑定 task/op/revision/截止/随机 nonce。它是**会打开本应用的公开 fallback**，
   不冒称不切应用的后台回调。Root 须把 onCreate/onNewWant 接到同一私有审批
   owner，核对票据全部字段，review 只展示，approve/deny 才调用 decide。
   真正无需切回应用的 LiveView 回调仍需取得权益并按官方可用回调组件集成，
   不以普通通知 START_ABILITY 冒充。
5. decide 只能接受同一票据 nonce、当前 revision、原短租约及未过截止的一次
   本地决定；拒绝/到期/旧回调不执行。远端传来的“已批准”不是本地决定。
6. execute 先 durable attempted，检查人输入 fence，再交真实 provider 一次。
   异常/中断/收据缺失保存原 unknown；再次执行原 op 拒绝。响应最多称
   input_acknowledged，effect_verified 恒为 false；任务完成仍由一次后续实际
   观察/原业务回执核验，绝不把输入投递当业务成功。

审批 surface 只展示有界 summary，不含输入文本、token 或截图。完整动作与待输入
内容应在本人可审阅的私有确认 UI 展示后决定；没有该详情入口时不要给 set_text
开放快捷批准。现有单次票据不推断永久全局授权。

## 集成和验证状态

本阶段只有合成 provider 的同源核心测试，不证明真实手机树、动作、截图、
LiveView 权益、通知点击或后台运行已验收。商业 SDK 构建/manifest/签名/安装/
系统设置及真实验收由 Root 串行完成。本目录没有注册新的远端工具网络协议。

必要测试通过 Click Evidence 的绝对 workdir 执行：

```sh
click-gate verify -- node --test apps/harmonyos/entry/src/main/ets/control/control.test.mjs
```

合成用例覆盖：审批不能自授予、nonce 与原任务绑定、durable attempted 先于
provider、旧 ref/过期/拒绝失效、独占短租约、人工输入在 commit 中抢占、
unknown 冷恢复和禁止重放，以及 provider/人工输入观察未验时拒绝动作。
