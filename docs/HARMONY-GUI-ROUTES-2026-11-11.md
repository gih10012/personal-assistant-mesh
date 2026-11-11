# 鸿蒙 7 手机操控路线核实与调整

## 本人当轮追问后的补正：手机内 HDC 可以免 USB

本人追问“能否通过手机内应用实现不接 USB 调试”。**可以实现这一设计，
无需把 HDC 客户端固定放在电脑；商业真机成功仍需实际验证。** 此前从某个
Relay 项目的 USB 初始化要求，推成所有 HDC 路线都需要电脑，排除得过早。
这条路线与无障碍及华为 GUI 合作路线相互独立。

- [ClawMate / mobiinfra-oh](https://github.com/doulujiyao12/mobiinfra-oh/blob/49fc716ab6775642f96a44a499b12e772a4af5d0/README.md)
  已有 ArkTS SelfHDC 源码：用户在手机开启无线调试并填写端口，App 直接连接
  本机，通过自身生成的 RSA 身份接受系统信任授权，再执行 Shell 控制。
  [客户端源码](https://github.com/doulujiyao12/mobiinfra-oh/blob/49fc716ab6775642f96a44a499b12e772a4af5d0/entry/src/main/ets/utils/selfhdc/HdcClient.ets)
  真实实现 TCP 分包、握手、授权和 Shell，不复制电脑调试私钥。
- [华为 HDC 文档](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/hdc/)
  的无线连接流程没有 USB 初始化前置条件。不能套用某些旧教程或 Android
  的配对码步骤；按手机实际显示的连接端口和系统信任弹窗处理。
- **证据边界**：该项目[引入 SelfHDC 的提交](https://github.com/doulujiyao12/mobiinfra-oh/commit/b564c6dc6a87946b168fd9b9ae66275838ed4e09)
  明确当时未构建、安装或真机测试。README 用法不是我们的设备验收。
  实现还不支持其检测到的 TLS 加密通道；实际握手决定兼容性。
- **本机连接不是本机监听**：公开 [hdcd 实现](https://github.com/openharmony/developtools_hdc/blob/8efffcf2fa2f5d4f997f2de8eb287432959417c8/src/daemon/daemon_tcp.cpp#L135)
  监听所有 IPv4 接口。AMITA 只连127.0.0.1，并不能证明校园 LAN 上不开放
  hdcd；商业固件是否可限制监听仍未证实。host 的 -s/-e 和 hdcd 的日志 -l
  不是手机回环监听开关，不能编造此配置。
- 关闭无线调试会关闭该调试通道；重新开启可能换端口，重启后的自动恢复
  没有承诺。示例只申请短暂后台延迟，不证明长期后台常驻。

**新增优先切片**：在 AMITA 内实现默认关闭、本人主动建立的 SelfHDC 会话，
生成独立密钥，优先回环地址，走系统调试授权；先核握手及一次只读截图，再接
已有屏幕租约/审批/人输入礼让与任务协议。远端仍通过 AMITA 认证出站通道，
不向远端提供原始无约束 HDC/Shell。没有监听隔离证据时，不自动长期启用校园
无线调试。手机独立 HDC 可行性、系统监听隔离、后台生存是三个不同验收项。
下面关于电脑 HDC 和旧 Relay 的限制只适用于其各自实现，不排除此新增路线。

2026-11-11。本人要求查 GitHub、其他开发者平台及 7.0.0.111 的新能力。
本轮为公开资料与本地商业 SDK 的只读研究；手机已拔，不运行 HDC、不操作
设备，也没有向华为发送合作邮件。两名 Sol/high 专员分别调查实际实现和官方
新接口，Root 复核关键原文。以下结论替代“继续在设置里寻找开关”的当前路线。

## 当前卡点

已经完成合法覆盖安装、真实登记挑战与独立结果校验、Asset 保存成员凭据、
真实前台出站联网。0.2 包及后续登记探测包实际覆盖安装，原应用身份保留。
实际公有查询显示 PhoneControlExtension 已安装、未启用；设置中只找到系统
内置辅助功能。没有取得 onConnect、跨应用树或输入动作的正向证据。
这不是手机未入网，也不能把“已安装”“构建通过”当成 GUI 可用。

当前判断：**普通应用的商用启用路径/平台能力准入是阻塞点。** 此前仅凭商业
SDK 仍保留旧声明，继续投入设置导航，依据不足。没有新固件或官方答复等新
证据时，不重复同样的菜单探索、manifest 调整或改 target SDK。

## 已核证据

| 来源 | 实际发现 | 对 AMITA 的含义 |
| --- | --- | --- |
| 华为 [Accessibility Kit 简介](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/accessibilitykit-overview/) | 当前正文将能力限定为辅助状态查询、事件发送，明确排除通过节点查询获得界面节点和屏幕内容 | 当前公开 Kit 不能直接充当 Android AccessibilityService 式通用控制后端 |
| 华为 [英文 Extension API](https://developer.huawei.com/consumer/en/doc/harmonyos-references/js-apis-application-accessibilityextensionability/) | onConnect、事件、按键回调 API12 起废弃，并额外说明相关系统能力不再可用；中文页仅写废弃 | 记录中英差异；SDK 声明和可安装不能证明回调可用，也不据翻译断言每台固件必然相同 |
| 开源项目 [cocoper/ad-blocker 真机记录](https://github.com/cocoper/ad-blocker/blob/main/docs/evidence/g0-probe.md) | 另一 API26 商业鸿蒙7设备同样能安装、正确解析 metadata、枚举自身，却没有服务启用入口 | 外部同类复现，支持平台准入方向；是作者报告，并非我们独立复验，也非所有机型的定论 |
| OpenHarmony [固定提交的动作 IPC handler](https://github.com/openharmony/accessibility/blob/3ffbe3513f07cb1df2bd98d74089582dff5eb33a/common/interface/src/accessible_ability_channel_stub.cpp#L337) | HandleExecuteAction 检查系统应用身份；旧 performAction 调用也可到达该 handler | 旧函数名不意味着避开底层权限；开源实现不等同于华为商业固件。不能据此概括全部读树/手势路径 |
| 华为 [系统 Agent GUI 机制](https://developer.huawei.com/consumer/cn/doc/service/agent-gui-0000002680521240/) 第5–6节 | 应用伙伴可以申请复用系统 GUI 能力，应用、功能、任务范围由双方确认；第4节 AI_Operation_Mode 只是状态查询 | 有官方合作路线，但未公布普通个人项目自助准入及通用跨应用控制 API；尚未申请或取得权限 |

Root 通过官方文档内容接口实际取得简介正文：`code=0`、版本 V173、
updatedDate 为 2026-11-10。公开网页有时只返回导航，不能把导航条目当正文。
本地只读资料快照私存，不把设备标识、凭据、聊天或原始手机截图入库。

本结论已实际安装到电脑 Worker、VPS Leader 与 Claw 的现有角色文档位置，
保留旧文件备份并加入下次相关工作前的阅读指引；没有重启运行时或启动模型
验收，`model_read_acknowledged=false`。Claw 汇报已提交，当前回执为
submitting/outcome_unknown，未宣称送达；沿原 request_id 查询，不换 ID 重发。

## GitHub 方案为什么不能直接移植

- [Open-AutoGLM 的鸿蒙实现](https://github.com/zai-org/Open-AutoGLM/blob/main/phone_agent/hdc/device.py)
  实际执行 HDC 的 `shell uitest uiInput`；点击、滑动等来自测试通道。
- [hmdriver2](https://github.com/codematrixer/hmdriver2/blob/master/hmdriver2/_client.py)
  通过宿主 HDC 上传 agent、启动测试服务、建立转发。
  [华为 UITest 指南](https://developer.huawei.com/consumer/cn/doc/doccenter-testing/uitest-guidelines/)
  限定由 aa test 拉起的测试 HAP，不能把 TestKit 导入普通后台应用就获得权限。
- [手机出站 HDC Relay](https://github.com/CCDawn/harmony-codex/blob/main/docs/remote-hdc-relay-plan.md)
  有实际代码及作者的真机结果，但先要求开启 hdcd TCP 端口；没有证明该监听
  仅在 loopback。作者也报告后台 helper 会暂停，后来采用手动打开应用恢复。
  不符合本人“校园无线调试不长期开放、正常使用手机时自治”的既定要求。

HDC 仍可用于本人接线时的开发调试。它不是当前常驻生产方案；用户没接手机
时不重复探测，更不为研究重新开放无线端口。

## 7.0.0.111 与 API26

手机系统尾号不等于新的 SDK API 版本。未找到 Pura80 Pro 这一具体机型
`.111` 的官方完整更新说明；不能据其他机型新闻承诺解锁 GUI 或后台常驻。
实际商业 SDK 和官方开发资料可确认以下机会：

| 路线 | 版本/前提 | 当前决定 |
| --- | --- | --- |
| [端侧 A2A Server](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/hmaf-a2a-protocol/) | createA2AServer、任务状态及产物回传 since26.0.0；AgentExtensionAbility since24。需平台关联、AgentCard及真实测试发布 | 优先将系统小艺作为 AMITA 入口；保留原 Mesh 协议、任务和权限，不把 A2A 当别的 App 的屏幕权限 |
| [Skill 开发规范](https://developer.huawei.com/consumer/cn/doc/service/skill-development-standards-0000002592931546/) | 可配置私有/系统/公开可见性；终端 ETS 仍受 HAP 签名与系统权限，Shell 在受限沙箱 | 梳理可复用结构化能力；不能宣称 AMITA 自然获得全部系统 Skill 或任意 Shell |
| [后台特殊场景授权](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/js-apis-resourceschedule-backgroundtaskmanager/) | API26 新授权弹窗只针对特殊场景；需相应 ACL，并匹配实际业务类型 | 不用于伪造任意 AI 常驻；现有 WorkScheduler 有界维护继续单独记录实际回调 |
| 系统/应用公开 API、意图 | 各自权限、范围、前后台条件及目标应用接口 | 优先做可靠工具，逐项真实公告能力；用户授权不改变操作系统准入 |

## 下一步及真正需要手机的部分

1. 保留旧无障碍适配代码和原 pending/unknown；能力维持不可调度，旧输入不重放。
   当前不会为重复设置试探或不相关测试叫本人接线。
2. 优先做上方 SelfHDC 的本机连接切片；同时接通实际系统 API/应用意图，推进
   端 A2A 与小艺入口；它们是独立可交付功能，
   不能冒充通用手机 Computer Use 已完成。
3. 准备官方 GUI 合作申请，询问个人开发者是否准入、调用接口、允许的跨应用
   范围、后台窗口和人工抢占机制。得到实际答复后再决定生产适配。
4. `.111` 更新后，只有可验证的新接口/入口证据才值得一次针对性设备核验。
   实际后台回调、独立业务任务、认知维护 Agent、实况窗权益和同签名无线原生
   升级仍分别待完成，不能由本次资料调查改成成功。

## 官方合作申请草稿（未发送）

收件渠道：官方页面公布的 hagservice@huawei.com。
主题：`【系统Agent生态合作申请】AMITA`

应用：AMITA（当前系统安装名 Mesh Node）
Bundle Name：`org.personalassistant.mesh`
开发者名称/联系方式：由本人确定，不推断或自动填入私人资料。
申请类型：系统 Agent GUI 能力合作及个人开发者准入咨询。
场景：本人手机作为个人多设备 Agent 网络节点，接收本人明确授权的任务。
优先使用公开系统 API、应用意图和 A2A；仅在结构化接口缺失时，申请已确认
目标应用和功能范围内的辅助执行。保留本地知情、暂停/取消、人工操作优先
与高风险操作确认，不请求系统签名伪装或绕过目标应用风控。

需确认：个人/非上架测试应用是否可参与；所需资格及测试发布路径；提供哪些
观察/输入 API；可否在约定范围跨应用；息屏和后台执行条件；是否可将授权
能力通过本人 AMITA 任务协议调用；升级7.0.0.111是否改变第三方辅助服务入口。

本草稿可直接补充后申请，未发送邮件，也未声称有合作授权。
