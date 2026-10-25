# 旧 AA 转达通知的单独处理

这是一个固定、不再适用的控制通知case，不是通用unknown解除器。原通知
只要求转达AA模型/effort偏好，不允许改配置/恢复当时暂停的开发；后来
本人已明确新建完整Goal并实际同步更完整的治理/角色文档。该旧尝试仍
只有原生开始/输入，没有自然完成证明，不宣称零效果或成功转达。

`scripts/review_superseded_notice.py`默认只读。仅固定case的ID/输入/
thread/turn/auth-home摘要可进入；Root先停止原cloud-worker，再绑定
原profile的只读thread/Goal元数据及原任务/session/source版本。apply
须携带原epoch和本次review digest，事务内再CAS，仅把旧尝试标记为
failed/deferred/superseded、epoch+1并保存私有beforeimage和回执。
原checkpoint字节、true效果保护、input/context、native session、artifact
和历史全部保留；UNKNOWN不改为成功，不重排旧输入，不发送旧通知。
原生元数据请求在事务外完成，SQLite写事务不等待原生进程响应。

旧核心claim会检查同scope所有非completed记录，所以仅改failed仍会
阻塞新消息。`assistant_mesh.superseded_notice.make_store_class`保持原
Store和构造参数，只在claim的两种已知reservation查询中识别该case的
完整、匹配、不可变审结回执，允许它停止占用会话。它不改原true标记，
不隐藏普通查询里的unknown，不让旧行重试；其它未审结记录仍阻塞。
没有回执、回执/行被修改、case不匹配或未知查询格式，沿原规则保留阻塞。
后续无关任务更新session不会使已经审结的旧通知重新占用会话。

Root将适配类装入原唯一receiver的existing factory；不新建Store/接收器，
不替换冻结核心或覆盖已有媒体修复。审结前后原始状态和恢复记录私存，
不发布聊天、认证、native history或完整review输出。应用后须分别记录
“旧通知审结”“新请求已由真实Leader领取”“模型执行/业务完成”。

原专员额度停止后，Root沿原分支接手，补齐user-unit观察与事务边界，
以及必要的窄reservation例外。六项针对性unit通过Click执行，单个
targeted shard约0.22秒、未复用；没有运行旧大套或原业务/模型试验。
覆盖单行变化/unknown/session/历史保留、错误case/版本/运行态拒绝、
CAS冲突和重复拒绝，以及legacy/modern claim查询、其它guard和回执
不匹配仍阻塞。生产应用及真实领取另记，本文本身不代表已经恢复。
