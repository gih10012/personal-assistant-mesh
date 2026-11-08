# 公开无障碍 provider 与真实全屏截图

本增量增加 `AccessibilityProvider.ets` 和 `GlobalScreenCapture.ets`，没有修改
共享 ControlSession、入口、manifest、原 Mesh/任务协议、凭据或生产配置。
它们直接调用指定商业 API26 SDK 现有的公开声明，不包含 SDK stub、系统权限
申请、服务自启、HDC 或测试框架后门。SDK 构建和真机启用/验收由 Root 串行。

## 窄接线接口

Root 在实际 AccessibilityExtensionAbility 的 owner 进程创建一份 provider，
传入已有持久 node_id 和当前 ControlSession 的活动撤权回调；不要生成替代身份。

```ts
const provider = new AccessibilityProvider(nodeId, (_reason: string): void => {
  if (session !== null) { session.ownerActivity(); }
});
// 同一 owner 中创建 ControlStore / ControlSession，并 await initialize()。
// 实际系统回调中转发：
await provider.onConnect(this.context);
provider.onAccessibilityEvent(event);
return provider.onKeyEvent(event); // 恒为 false，按键继续给本人使用。
provider.onDisconnect();
```

上述是接线步骤，不是把它们依次执行的脚本。只有实际系统 onConnect 才调用
provider.onConnect；不人工伪造事件或回调来解锁动作。provider 首次真正读取
当前 root/bundle/window 成功后才将 root_verified 设为 true。root 查询失败会
清除证明、报告固定错误与 SDK 数字码，inspect/act 不可用。

正常动作还有三道独立门槛：本人本地设置 `provider.setOwnerControlAllowed(true)`、
真实事件/按键回调至少已有一份证据，以及 ControlSession 自身的本地启用、
原 task 独占短租约、当前观察与一次审批。任何远端 dispatcher 都不能暴露
这些本地 setter、伪造回调或直接绕过 ControlSession 调用 act。

`provider.principal()` 返回 `node:<已有node_id>`，用于 source 原生 principal；
ProviderView 的 bundle/window/ref 是本机观察元数据，不替换 Mesh principal。

## 观察与动作

- inspect 只读当前 active root，BFS 最多输出 128 节点、访问 256 节点、深度 12。
  到预算或深度边界返回 truncated。真实的 AccessibilityElement 对象只留本机
  Map；每次观察生成新 UUID view_token，事件 generation 变化使所有句柄失效。
- 树以实际 default display 尺寸返回全屏像素边界，只接受主 display 0。不可见
  或完全在屏幕外的节点不输出。没有虚构坐标、任意点位或随意选择其他窗口。
- 必要属性缺失/读取失败明确报 unavailable；密码标记缺失时仍当敏感节点，
  不读取内容、不开放动作。非敏感节点的 label 有界，原生动词采用实际 actionNames。
- 动作前重新读取 active root/bundle/window、原对象 componentId/role/label/rect、
  可见/可用/密码状态与 display 几何，再核 snapshot token 与人活动 fence。
  不把旧列表编号当对象，不用错误窗口或发生变化的目标。
- 当前与共享 ControlSession 对齐的工具动词为 `tap`、`set_text`、
  `scroll_forward`、`scroll_backward`，分别调用原生 `click`、
  `setText({setText: value})`、`scrollForward`、`scrollBackward`。
  原生 `focus` / `accessibilityFocus` 的固定映射也已实现，但不会放进当前
  四动词的 inspect.actions；Root 将来需先显式扩展审批协议才可从 dispatcher
  到达，不能用额外接口绕过当前共享核心。
- GesturePoint/GesturePath/injectGestureSync 也是实际 SDK 的公开旧 API。
  fallback 默认关闭，需本人本地单独允许。只有语义 action 未 advertise，且
  真实 clickable/scrollable 属性及完整屏内几何满足时，才在**第一次 effect 前**
  选 tap 或局部纵向 swipe 一条路径。已调用 performAction 后不论抛错还是未确认，
  绝不再试 gesture，避免重复效果；权限和 API 能力失败均保留错误。

所有输入结果只证明 native admission/acknowledgement，不证明业务完成。
ControlSession 仍在外层保存 attempted/unknown，原 operation 不自动重放。

## 真实活动 fence 的精度

provider 对每个实际收到的 onAccessibilityEvent/onKeyEvent 都撤销句柄、短租约
与待执行 fence，不读取事件中的文本。物理触摸、程序化 UI 变化及自动输入可能
无法区分，故 status 明确返回 `full_physical_touch_coverage=false`，不会声称有
全局准确触摸监听。availability.human_input_observable 表示实际 callback 已见证，
其精确语义为 status.input_observation 的“所有已投递事件/键，保守处理”。

保守处理可能让一次已投递输入因紧随其后的界面事件记为 unknown；此时看原结果，
不要因为模型未拿到成功就再发一次。审批 UI 自身若改变 active window 或触发
活动事件，也会使旧观察失效，不能偷偷忽略这些事件让旧票据继续执行。Root 的
无需切应用审批须使用真实可用的可信回调 surface；START_ABILITY 的前台 fallback
会切回应用，不能冒充对原跨应用窗口无影响的后台决定。

## 全局截图与选择截图分开

`SelectedScreenCapture.pickFromOwnerGesture` 仍是用户交互选区工具。
`GlobalScreenCapture.captureApproved` 则真实调用 `screenshot.capture({displayId:0})`：

1. Root 按官方真实配置声明 normal/user_grant 的 CUSTOM_SCREEN_CAPTURE、reason
   和 usedScene；只在本人 UI 手势调用 requestPermissionFromOwnerGesture。
   不使用 system_basic 的 CUSTOM_SCREEN_RECORDING 代替正常授权。
2. 本人本地启用 setOwnerCaptureAllowed；每次 captureApproved 接收当前有效
   fence 及不超过 30 秒的本地审批截止，再实际核系统权限，调用期间与返回前也
   再核授权。没有权限、被撤权或平台 801 时明确失败，不打开 picker 当替代成功。
3. 一次只运行一个调用，最短间隔一秒；图片缩至 1280 边长、1MP 和 256KiB
   JPEG 预算以内。返回 crop_origin=(0,0)、真实缩放和 `user_selected=false`，
   不写磁盘、不自动上传、不调用额外模型。
4. status 只有真正成功 capture/编码后才设置 platform_capture_verified。
   这是可重复的单次全屏观察接口，`continuous_stream=false`；没有宣称已实现
   连续视频、后台保活或已获真机授权。进程/系统生命周期仍由真实宿主决定。

此次新增均只提供代码与接线点。真实商业构建、onConnect/属性/动作/gesture、
用户权限及前后台截图尚待 Root 集成验收。既有六项核心测试未改，不重复跑全套；
本增量只做必要的 Click diff 检查，平台正确性以 Root 的实际 SDK/真机结果为准。
