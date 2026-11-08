# 背景决定与原窗口重验

`BackgroundDecisionBridge` 在实际 service onConnect 订阅公开 common event，
指定 `publisherBundleName=org.personalassistant.mesh`；disconnect 取消订阅，
generation 防止异步订阅在断开后复活。每份回调还核系统 `CommonEventData.bundleName`，
只接受本包，绝不放宽到任意 system publisher 或信任 payload 内自称的身份。
许可/拒绝使用本包创建的 immutable、one-shot `SEND_COMMON_EVENT` WantAgent；
创建后实际调用 getBundleName 核 creator。本包目标限定在 Want.bundleName；review
仍用 START_ABILITY。回调核固定 event、原 approval/nonce/op/task/revision/截止及
本机原 grant，Source 原 WorkRecord 必须仍 prepared、未 invocation、没有结果。

订阅成功、agent creator 成功与实际按钮回调成功是三个状态：
`background_decision_subscribed`、`background_agents_verified`、
`background_decision_callback_verified`。没有最后一项的真实证据就不宣称背景
按钮已验收。SDK 若不给 publisher 或实际 publisher 为 system/其他包，原样拒绝，
状态记录固定错误及观察到的 publisher；若系统过滤使回调根本不投递，则仍为
callback 未验证，不以超时或任意 system 事件补造成功。无新系统权限。

普通活动仍立即清旧 ref/lease/fence。未尝试的 action 票据仅变为 dormant pending，
原截止与 Source preflight Promise 不延长，不自动执行。默认 decideBound 保持原
严格语义。可信本人新的明确决定才可走 decideAfterRevalidation：重新取 root 和
完整有界非 truncated view，比对原窗口、几何、所有节点及原元素 native 状态
fingerprint（包括 checked/selected/focus 等），仅排除新生成的 transport view_token。
完全相同且 Source 仍 prepared，才保存原 op approved 并授予新的本机 revision/
短租约/fence；原 task/op/request/nonce/票据 revision/截止不改。任何期间或后续活动
再次撤权。候选和新本机证明只在当前进程；冷启动取消未决/approved，unknown/
attempted 永不重新授权。无完整 fingerprint、原树截断、窗口/语义状态变化均拒绝。

`set_text` 的 Root UI 窄 seam 是 `PhoneControlBridge.markPendingReviewed(uiContext)`：
本人先在私有详情页看完全部输入，再明确点击“已核对完整内容”。它只记录 exact
ticket+input 的 review，不批准、不重验、不执行；然后返回原 App，再在实况窗/
通知中明确点“允许这一步”。若决定发生时仍是详情页/通知系统窗口，重验拒绝并
保留原待决定票据，必须回原窗口后再次明确决定，绝不自动等回去偷偷执行。
Root 需给完整正文页接这个按钮；截止前未完成就失效，不能声称所有 set_text
场景已可用。普通硬件决定仍只是 fallback，不作为实况窗已完成的证据。

本提交只有新增的窄 Node 校验：发布者不可冒充、完整重读与原上下文不变、窗口/
几何/checked 状态/截断不一致拒绝、Source 非 prepared/冷恢复/unknown/到期拒绝、
重读/提交中人输入撤权、dormant 不能自授新 fence。没有重跑旧广泛套件或运行
共享 SDK；商业编译、真实通知/实况窗 sender 与按钮、完整 review 回原 App 后的
实际动作仍由 Root 串行验收。公开 SDK 依据：
[发布者过滤](https://developer.huawei.com/consumer/en/doc/harmonyos-references/js-apis-inner-commonevent-commoneventsubscribeinfo/)、
[CommonEvent API](https://developer.huawei.com/consumer/en/doc/harmonyos-references/js-apis-commoneventmanager/)。
