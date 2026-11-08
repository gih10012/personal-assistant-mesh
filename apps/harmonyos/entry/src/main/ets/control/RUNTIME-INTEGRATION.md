# 实际无障碍服务出站接线

Root 将 `PhoneControlExtension` 声明为 `type: accessibility`（同一 bundle 下不要
同时运行 probe 和 runtime 两个控制 owner）。由本人在系统设置显式启用；代码
没有自动启用、USB/HDC、假 foreground 或保活组件。真正的 onConnect/onDisconnect
控制 service active；没有回调或 root 失败时不公告 GUI。

Runtime 读取现有 `mesh_node_v1` 的原 node_id、已绑定 `mesh_work_v1` 的公开 config
与原 Asset `phone:node` 凭据，不复制/重发凭据，不改普通 UI session。它只在自己的
私有 Preferences `amita_control_runtime_v1` 生成并保存独立 control_session_id，
复用 WorkEngine/WorkSnapshot 作为唯一 Source operation journal。身份会显示在
`PhoneControlBridge.status(context)`，Root 据此配置同一原节点的后端 installed adapter。
后端未启用或 control session 不匹配时如实失败，不回退到普通 UI session。

主 UI/Entry 的窄入口（仅本人本地调用，不注册为远端工具）：

- `PhoneControlBridge.configure(uiContext, PhoneControlOwnerOptions)` 配置 enabled、
  gesture_fallback、screen_capture 和已经取得权益的 liveview_event（默认空）。
- `ownerToggle(uiContext, boolean)`；同值不改 grant，真正改变配置生成新 grant_id。
  Source buffer 回传也核原 grant，关掉再打开不会默默发送之前撤权的私有结果。
- `status(context)` 返回不含 nonce/输入正文的运行状态；`pendingForOwner(uiContext)`
  供私有详情 UI 展示当前原 task/op/epoch/截止和完整待输入内容。
- `decidePending(uiContext, 'approve'|'deny')` 只用于本人已看清完整内容后的本地决定。
- `onNewWant(uiContext, want)` 接在 EntryAbility 的 onCreate/onNewWant；它核固定
  本包 action 与原票据字段后写入本机 decision mailbox，review 不批准。该公开
  fallback 会切换应用，旧目标若被活动 fence 撤销就不能继续，不隐瞒这个限制。
- 截图权限另由本人 UI 手势调用 `GlobalScreenCapture.requestPermissionFromOwnerGesture`；
  Root 按 SDK 声明 CUSTOM_SCREEN_CAPTURE/reason/usedScene，不申请系统权限。

唯一输入 JSON，与后台 CLI 一致：

```json
{"format":"amita-phone-ui-control/1","command":"inspect_ui","max_elements":64}
{"format":"amita-phone-ui-control/1","command":"perform_action","element_ref":"原观察ref","action":"tap","text":""}
{"format":"amita-phone-ui-control/1","command":"screenshot"}
```

selection.action 分别为 inspect/action/inspect；workload 只有 input Blob，原始
完整 UTF-8 JSON ≤64KiB，严格核 base64/长度/hash/固定字段，不能传 task/op/epoch
覆盖 dispatch。动作只允许共享审批核心的四个原生动词。

inspect_ui 复用本人的全局控制授权、真实 OS 服务及 Source 原精确 grant，不额外
弹逐次审批。screenshot 再核独立开关和真实系统权限，调用全屏 capture；Mesh 模式
缩至 640px 边长、256K 像素、42KB JPEG，超出仍明确失败，不截断或用空图成功。
所有输出是完整 JSON Blob ≤64KiB，含原 task/op/epoch 和 `node:<node_id>` principal。

动作 preflight 发生在原 WorkRecord prepared 已 flush 后、任何 Source accept/start
之前，复用 ControlSession 原 op 的一次票据。Root 的 WorkEngine hook 返回后刷新
permission_context 并 flush，再 allowed；hook 失败保留原 row unknown，重投只查原
记录，不重弹/执行。reportAllowed 独立于旧 ref，允许同一已授权完成 buffer 对账，
仍受本人原 grant 限制。已消费票据不能用于另一调用；未知只对账不重做。

只有发布成功的明确 pending 审批才武装硬件决定键：单独音量＋的首次 DOWN 允许
这一步，音量−拒绝；精确决定先处理，不先无条件清 pending，并消费同一按压的
配对 release。已经按住的键、组合键、没有 pending、review_required 的音量＋及
普通输入照常交给系统并 fence，不劫持日常音量。set_text 需要完整私有 review，
不能仅凭摘要音量盲批。LiveView 无权益使用真实系统通知，通知失败则明确
in_app_pending，保持原等待直到拒绝/到期，绝不 autoapprove。

notificationUpdate 计数但不是物理输入，不因发布审批通知自毁；其他窗口/触摸/
焦点/未知事件仍保守撤权。full_physical_touch_coverage 仍为 false。这里只消费
SDK 真实 onKeyEvent 的明确决定键，没有伪造触摸空闲或绕过系统设置。

Root 负责共享 WorkProtocol 的可选 preflight/reportAllowed、UI/Entry/module、
Source adapter 开关、商业 SDK 编译及真机验收。本提交未做这些共享变更；新增
本机 gate 三项 Node 检查，旧六项未改、未重复跑。平台编译/按键/后台连接仍待验。
