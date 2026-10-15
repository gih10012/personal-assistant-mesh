# 隔离 unsigned 构建实测（2026-10-15）

在 `e2afb67` 首片基础上，新叶 `work/harmonyos-unsigned-build-20261015`
完成了一次**真实 OpenHarmony 26 原生 ArkTS/资源/HAP完整构建**，不是
Node测试或类型stub。**尚未完成华为商业 HarmonyOS SDK 构建、手机安装、
真机API/后台验证、入网或小艺接入**。本叶只修构建实际发现的最小问题。

## 来源、审查与配置

- [DevEco官方入口](https://developer.huawei.com/consumer/cn/deveco-studio)
  提供 `@deveco/deveco-cli`；已读取 npm 的 wrapper/common 1.3.5 metadata、
  脚本和README，校验完整两包的registry SHA512。Linux路线明确要求
  CLT26和 `DEVECO_CLI_CLT_PATH`。未执行该CLI或安装Skill/MCP，不运行
  `init`、登录、遥测、后台文档初始化或自动更新。stable1.3.0包下载
  超时，其部分文件保留，不当作已核验安装源。
- [官方26.0.0版本表](https://developer.huawei.com/consumer/en/doc/harmonyos-releases/deveco-studio-new-features-2600)
  对应 release CLT26.0.0.821、Hvigor6.26.4、ohpm26.0.0.630、Node24.14.1，
  modelVersion及compile/target新格式为 `26.0.0`。本工程已更正首片中的
  `7.0.0(26)` / modelVersion5.0.0，不表示已取得商业SDK。
- 官方下载中心公开JS的下载链有登录/联盟协议、实名和待签SDK协议处理；
  匿名只读metadata请求实际未获得下载资格（CSRF前置检查失败）。未
  构造凭据、代签、使用账号UI或绕过检查。商业CLT/HMS下载/本人协议
  交互是独立gate；这里只继续公开OpenHarmony路线。
- [华为云官方公开OpenHarmony7 SDK目录](https://repo.huaweicloud.com/harmonyos/os/7.0-Release/)
  的 `ohos-sdk-windows_linux-public_20260829.tar.gz` 已完整下载，
  `3,671,263,766 B`，与同目录官方SHA256一致：
  `6bf6ae1efe8de0e8bd15ddbd7fac58bcb54d9620262a541b9b219439317a4c42`。
  只解出Linux的ets/toolchains/js/native/previewer组件；组件metadata实际
  为platformVersion26.0.0、version26.0.0.38，http.d.ts确有maxRedirects。
  没有下载71GB系统源码、Windows工具或镜像，没有启动previewer/模拟器。
- [官方Hvigor包](https://repo.harmonyos.com/npm/@ohos/hvigor)及
  [官方插件包](https://repo.harmonyos.com/npm/@ohos/hvigor-ohos-plugin)
  6.26.4，按registry的真实HTTPS重定向取官方dbankcdn文件，校验registry
  SHA512后解包其bundled依赖。未执行npm install或lifecycle脚本；工具
  原字节未改。两tgz SHA256分别为
  `33b2741aca3ee00f6375d6a988b4951875a0c2369d0b0ce3568a6d69249ad82d`、
  `2f97a309bad4297a478091278ed6372c18330f1159551427529436c3525779b6`。

宿主Node26.8.2/Java26.0.2.1实际可完成本次工具链，不更换宿主版本。
推荐Node24.14.1的官方包下载超时，仅保留部分文件，未执行。所有工具、
源码副本、日志及生成文件在仓库外私有临时目录；未全局安装、修改npm/
代理/系统配置或认证。构建使用无网namespace、隔离的同路径个人home、
`--no-daemon`，不改变HOME/CODEX_HOME值、不接触真实Codex历史/账号。
此隔离只作用于本次构建子进程，不限制任何Mesh节点/原生agent能力。

## 真实编译带来的修正

1. PreBuild schema实际拒绝缺少的 `startWindowIcon` 和
   `startWindowBackground`；添加原图标引用及一个纯色启动背景资源。
2. CompileArkTS实际拒绝 `buffer.Buffer.byteLength`：本SDK Buffer提供
   `length` 字节长度。改为
   `exactByteView(utf8.buffer, utf8.byteOffset, utf8.length)`，仍只哈希
   原UTF8范围，保留slab哨兵测试，未改协议/输入/请求身份。

首轮缺SDK路径、缺组件、原module配置错误及原Buffer类型错误的日志都
保留。没有清除失败、改SDK类型stub、禁用ArkTS检查或把缓存跳过算编译。

公开SDK没有 `phone` syscap profile；仅**临时构建副本**使用下列overlay：

| 字段 | 手机工程保留值 | 公开SDK构建副本 |
| --- | --- | --- |
| runtimeOS | HarmonyOS | OpenHarmony |
| compatibleSdkVersion | 5.0.0(12) | 12 |
| module.deviceTypes | phone、tablet | default |

实际逐字比较了全部ETS/TS业务源码与两边Hvigor/package配置；只有上表
三项平台配置不同。没有为通过构建删除文件能力、替换加密/网络函数、
隐藏GUI/小艺缺项、伪造phone profile或扩大权限。HAP内metadata也确认为
compileSdkType=OpenHarmony、compileSdkVersion=26.0.0.38、deviceTypes=
default、target API26、minimum API12，所以不能拿它给Pura手机宣称可安装。

## 完整构建回执

在临时副本中，用已核验6.26.4工具的真实 `bin/hvigor.js` 执行：

```sh
node /PRIVATE/toolchain/node_modules/@ohos/hvigor/bin/hvigor.js \
  --no-daemon --no-incremental --mode module \
  -p module=entry@default -p product=default -p buildMode=debug \
  -p enableSignTask=false assembleHap
```

`NODE_PATH`指向私有解包的工具依赖，`OHOS_BASE_SDK_HOME`指向公开SDK；
用系统隔离包装上述子进程，运行目录是私有工程副本。手机工程本身不
以这个OpenHarmony overlay替换。不要把 `/PRIVATE` 当真实可执行路径。

- 实际exit0，`BUILD SUCCESSFUL in 3 s 506 ms`，33 tasks全执行、0缓存。
- CompileArkTS、CompileResource、PackageHap、PackingCheck实际完成；
  签名开关false、signingConfigs空。日志中的SignHap是无签名的hook空过，
  没有创建证书/签名、运行 `signature generate` 或调用手机。
- `entry-default-unsigned.hap`：163817 B，SHA256
  `dd351dfcb652bd710a52a0601f178b7747fd8ee209e3dae29539cce94ee27757`。
  ZIP完整性检查通过，含真实 `ets/modules.abc`、resources.index、module
  metadata与原生资源。无安装/运行证据；未做设备签名认证验收。
- 完整构建日志SHA256：
  `09492ca34b236ce3825f51cfd86ff597234add47d58adfc48c9e11c0e6b5d4a6`。
- 16条SDK异常/syscap警告保留：部分API会抛异常，默认设备profile不保证
  每台设备有Crypto能力。未关闭这些警告；UI调用路径仍捕获失败，不将
  unavailable/unknown改为成功。缺ohpm安装元数据的SemVer警告与未配置
  signingConfig也保留；不声称依赖发布/签名/量产手机能力已验收。
- 更正后原4项Python项目/协议检查＋30项enrollment回归共34项通过，
  0跳过（0.240 s）；其内包含12项实际共用TS逻辑的Node测试。不是将
  这些测试数量相加冒充33项系统任务或HAP/真机验收。

工具包、SDK、HAP和私有构建日志不提交公开仓库，不篡改原业务task/
nonce/journal，不请求真实能力grant。本叶未接relay或另造入网协议。

下一gate：本人取得匹配商业CLT/HarmonyOS SDK及协议授权后，对保留的
phone/HarmonyOS工程再次完整unsigned构建；确认真实手机版本/合法调试
授权后才能签名、安装、验证picker/网络/前后台和原Mesh挑战闭环。SDK
签名安装与生产入网由Root单运维，不因本公开SDK构建成功而自动执行。

## 原 relay 客户端固定版本再次编译

对最终客户端 `83bb76eb6938022f6d9d2f44db739572489feab4`（含HTTP前本人
共享撤权再核）合入上述 `021e482` 配置/资源，独立组装源码树 `752b653`，
没有改作者worktree。首轮真实无增量CompileArkTS拒绝4处比较：
NativeServices第330/334行，早期guard把result_state窄为not_submitted，
却看不到begin/observe helper已经写成attempted/acknowledged；第367行
同样看不到challenge_state的attempted/granted变化。

修正仅增加 `relayResultState` / `relayChallengeState` 两个纯读取函数，
让原生调用者在转换helper之后明确读取**同一原记录当前状态**。没有
强制类型转换、@ts-ignore、关闭检查、复制状态/替换ID，未改变早期阻重
guard、合法转换、unknown处理、单次POST或权限规则。新增同源Node用例
覆盖begin、accept、cold-resume、observe后读回，再确认原请求不能重做。

再次实际执行相同无网/空个人home、no-daemon/no-incremental、关闭签名
的完整构建，复用已核SDK/工具且旧工具目录只读挂载。独立新副本仍仅
采用上表3项OpenHarmony平台overlay，全部实际业务源码与审查树逐字
一致；不是借用上一份不含relay的HAP。

- 首轮失败原日志SHA256：
  `7a72fe9e2d1e04423737f3c32d498b83656803934e5edfdc95a9c5502b3ed2fa`。
- 修正后实际exit0，3.786s，33 tasks全执行、0缓存；CompileArkTS、资源、
  PackageHap与PackingCheck通过，17条异常/syscap警告完整保留。
- 新 `entry-default-unsigned.hap` 222032 B，SHA256
  `68eb0decef0d69040d7d62252407a9b4219eb4bf38691b554621896c468b27ec`，
  ZIP完整性检查通过。没有签名/安装/手机网络或API调用。
- 完整成功日志SHA256：
  `c56b01fa2658eea3c0083df4f975338a0ea17ef1329bafdd89709e450e278f6f`。
- 25项同源Node测试通过、0跳过；相关Python22项0.837s通过，其中真实
  authority/同TS JSON对齐不是NetworkKit/手机执行。Python另出现既有
  SQLite connection ResourceWarning，原日志保留，不抑制或冒称零警告。

首片SDK/旧HAP/所有旧失败日志未覆盖；新客户端产物与日志另存在私有
临时目录，不提交生成包。**商业HarmonyOS SDK、真实手机、TLS relay路由/
live-task validator/credential部署、完整入网仍未验**，不由编译通过推导。
