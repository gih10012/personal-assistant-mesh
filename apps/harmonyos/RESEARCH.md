# 鸿蒙手机接口核对（2026-10-15）

只依赖本轮实际打开的华为/OpenHarmony官方资料/官方仓库。原聊天中的
型号、API26与接口描述是需求背景，不代替SDK/真机证据。OpenHarmony
源码≠华为量产手机完整源码或允许刷机；本项目不改系统、不刷设备。

| 方向 | 已有一手证据 | 本片选择/未证部分 |
| --- | --- | --- |
| ArkTS Stage/UIAbility | [官方生命周期](https://github.com/openharmony/docs/blob/master/en/application-dev/application-models/uiability-lifecycle.md) 有前后台/销毁回调 | 原生UIAbility，后台SUSPENDED；未真机 |
| 用户文件 | [官方文件选择](https://github.com/openharmony/docs/blob/master/en/application-dev/file-management/select-user-file.md)、[Picker参考](https://github.com/openharmony/docs/blob/master/en/application-dev/reference/apis-core-file-kit/js-apis-file-picker.md)支持UIAbility context、DocumentViewPicker、临时只读URI | 本人选择一个文件，READ_ONLY；不请求全盘/自动持久授权 |
| 持久状态 | [Preferences](https://github.com/openharmony/docs/blob/master/en/application-dev/database/data-persistence-by-preferences.md)提供put/flush，XML适用单进程且不是加密存储 | singleton应用；intent flush后执行；不存token/URI，不宣称跨进程事务数据库 |
| SHA256 | [CryptoArchitectureKit](https://github.com/openharmony/docs/blob/master/en/application-dev/security/CryptoArchitectureKit/crypto-generate-message-digest.md) createMd/update/digest | 官方API适配源码；设备crypto runtime未测 |
| HTTPS | [HTTP官方API](https://github.com/openharmony/docs/blob/master/en/application-dev/reference/apis-network-kit/js-apis-http.md)有maxLimit、API23+ maxRedirects=0 | 限定本人HTTPS join、TLS默认校验、无redirect/全局代理改写；设备TLS/网络未验 |
| AgentExtensionAbility | [官方SDK声明](https://github.com/openharmony/interface_sdk-js/blob/master/api/@ohos.app.agent.AgentExtensionAbility.d.ts)确有API24+ onCreate/onConnect/onData/onAuth/onDisconnect/onDestroy、AgentRuntime.Core | SDK存在≠本机HAP可注册/系统小艺能路由；首片未声明extension |
| 小艺云A2A | [华为2026年8月月刊](https://developer.huawei.com/consumer/cn/monthly/202608)明确Remote Agent服务、AK/SK、平台配置与真机调试课程 | 系统小艺（非Claw）可作为独立适配研究路线；未建平台Agent/AKSK/网关/认证，不能声称已接Leader |
| HMS Agent Framework端A2A | [官方API差异页](https://developer.huawei.com/consumer/cn/doc/doccenter-release-notes/js-apidiff-agentframeworkkit-7002)搜索索引出现createA2AServer/updateStatus/addArtifact | 正文页面本轮JS空页；仅官方索引线索，缺完整SDK类型/权限/接入规则，不虚写可编译适配或宣称标准A2A互通 |
| Intents | [官方Intents Kit入口](https://developer.huawei.com.cn/consumer/cn/sdk/intents-kit)描述应用功能/内容给系统入口调用 | 不等于本App能调用所有App/小艺权限；开发平台配置/审核、端侧调用与连续语音需独立核 |
| GUI | [华为HDC2026正式公告](https://www.huawei.com/cn/news/2026/6/harmonyos7-hdc)确实宣布向开发者开放GUI操控 | 没有拿到本应用可用的具体API/资质和真机权限证据，保持not_verified，不写假的tap/screen hierarchy |
| 后台 | [官方Background Tasks](https://github.com/openharmony/docs/blob/master/en/application-dev/task-management/background-task-overview.md)明确可被挂起/回收且后台任务受限 | interactive节点；不伪装文件传输保活、不承诺24小时常驻 |
| AppService | [官方AppServiceExtensionAbility声明](https://github.com/openharmony/interface_sdk-js/blob/master/api/@ohos.app.ability.AppServiceExtensionAbility.d.ts)当前仅2-in-1并需企业ACL权限 | 不将PC后台组件当普通手机常驻方案 |

AgentExtensionAbility 的 API26 onAgentInvoked 在官方声明中标为systemapi；
不因同一个类有公开回调就把systemapi混进普通应用。小艺云/端A2A协议
都另需适配，不把本项目 `mesh-a2a/1` 私有协议标成Google/Huawei A2A。

实际阅读了官方 app_samples 的
[SandboxShare 工程结构](https://github.com/openharmony/applications_app_samples/tree/master/code/BasicFeature/FileManagement/FileShare/SandboxShare)：
它有AppScope/entry/build-profile/hvigor等真实Stage工程。该GitHub镜像
样例仍为API9/Hvigor2.3，不直接复制成HarmonyOS7版本保证；华为当前
[DevEco官方页](https://developer.huawei.com/consumer/cn/deveco-studio)提供
CLI与Linux模拟器信息，但IDE系统要求列Windows/macOS。原型结构采用
同一Stage入口，具体Hvigor/modelVersion/SDK26组合须实际工具链核验。
本片未复制上游样例实现代码，工程/图标由本项目新写，许可证沿仓库MIT。

本轮若干华为doccenter页面仅返回空JS壳/抓取timeout，官方SDK源码与
可读OpenHarmony文档补了公共API证据，但不据此推导华为商业系统拥有
全部相同权限。旧聊天里的OpenHarmony版本/镜像大小不影响首片构建，
未下载整系统源码。后续按真实手机版本/安装资质逐项验证，节点主体、
小艺入口、GUI与后台生命周期彼此解耦；不让不可用的某个adapter阻塞
已经授权的本机业务能力。
