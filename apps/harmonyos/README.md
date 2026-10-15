# HarmonyOS native Mesh node

2026-10-15：原生 Stage/ArkTS/HAP 工程源码，**尚未通过 Harmony SDK 编译或
真机验证**。本机没有 Hvigor、ohpm、hdc、Harmony SDK；没有下载或改装
工具链、配对手机、创建证书、签名、安装 HAP、使用真实凭据或生产网络。
这不是 WebView/手机遥控面板，也不是重新实现一套 Mesh。

## 现有源码

- UIAbility + ArkUI 原生界面；应用沙箱 Preferences 保存随机 node_id、
  原 enrollment、operation/nonce/request SHA、完整结果与 unknown。
  node_id 不取 IMEI、serial、MAC；卸载/清数据不被当成安全恢复途径。
- 同 `mesh-enrollment/1` / `mesh-enrollment-challenge/1` /
  `mesh-enrollment-result/1` / `mesh-adaptation-report/1`，复用原
  [入网合同](../../docs/ENVIRONMENT-ADAPTIVE-ENROLLMENT.md)与
  [bootstrap](../../docs/ENROLLMENT-BOOTSTRAP.md)。手机当前选 interactive，
  不是缺后台就伪装 persistent。
- 本人导入原 owner/enrollment/continuation；已有绑定变化拒绝，不清旧
  journal。HTTPS 加入入口只核原 `/v1/mesh/hello` peer/authority/协议，
  **路径可用≠登记或执行授权**。token 仅本次内存，不写 Preferences/日志。
  导入 repo pin 仅保留原合同绑定，不等于当前HAP的独立源码/二进制证明。
  认证请求要求设备 API23+，使用 maxRedirects=0 和64KiB响应上限，
  不跟随 redirect/自动换公网出口或关闭TLS校验。
- `challenge.bytes.repeat` 原生字节 recipe；`file.read.selected` 只读本人
  用系统 DocumentViewPicker 选择的一份文件，≤64KiB，完整读回/base64/
  SHA256，不截断。没有请求全盘、照片、联系人、定位、录音或系统权限。
  URI临时授权不落盘；选择文件与允许 Mesh 使用是两个明确步骤。
- 执行前持久化 attempted/原ID，完成后保存完整 provider-reported
  envelope。进程中断 cold-resume attempted→unknown；同ID只能读回，
  不重新读文件/执行、换nonce或换ID自动重试。旧unknown不挡无关新请求。
- ACTIVE/SUSPENDED、foreground/本地开关/文件临时grant动态能力状态；
  capability class/instance、source、scope、共享 app-runtime pool、故障域
  单独表达。后台撤销本片 Mesh 新调用开关，**不拦截手机任何原生功能**。
- 小艺/端A2A/GUI/外围蓝牙设备仅有独立适配研究边界，未注册extension，
  不凭发现耳机授予控制权。未内置模型，报告 harness.available=false。

当前 provider 由本机应用内明确导入原 challenge、执行、读回完整结果；
外层可通过已有 bootstrap 的 `record-result`/独立 oracle接入。**没有**
臆造现有 server 不支持的 enrollment REST URL，没有假装手机已注册或
Leader已经远程调用。后续在原 authority 接一个受限的 challenge/result
relay，保持同一contract与原nonce/结果，才能验收自动远程调度。

## 验证与构建

```sh
# 在仓库根目录；仅同一TS逻辑实际执行，不是ArkTS类型检查或HAP编译
node --test apps/harmonyos/test/protocol.test.mjs
python3 -m unittest tests.test_harmonyos_node -q

# 只检查当前工具/项目配置，不安装、下载或签名；缺SDK时exit2
node apps/harmonyos/tools/build-preflight.mjs
```

本机 Node26.8.2：12项 Node protocol cases、4项 Python项目/协议检查均
通过；后者覆盖同一 TS 与 Python 原 schema/hash Unicode字节精确一致。
同原 Python enrollment 30项相关回归合跑34项通过，0跳过。Node组含原生
适配实际共享的exactByteView：在带前后哨兵的Buffer slab里只哈希UTF-8
byteOffset/byteLength范围。平台源码检查另核同应用互斥/intent-before-effect，
不是实际设备并发测试。这几组有包含关系，不相加冒充独立系统验收。
Node23+/22.18+才能
直接strip TS；较旧CI明确skip该运行层，绝不算ArkCompiler通过。

当前 build-preflight 实际 exit2：hvigor=false、ohpm=false、hdc=false、
sdk_configured=false；compile_attempted=false、hap_compiled=false。
只有Node/Java不提供ArkUI编译器。所有 `.json5` 使用合法JSON子集方便
schema检查，但这不能证明SDK类型、装饰器、资源打包或手机生命周期。

本工程 default product 配置 compile/target `7.0.0(26)`，compatible
`5.0.0(12)`；网络认证适配另有API23 runtime guard。API26来自当前
官方 HarmonyOS7 开发资料，**工程版本字符串/modelVersion/Hvigor组合
尚须真实SDK解析验证**，不是已经被编译器接受的声明。`signingConfigs`
为空，无本机路径/证书/设备ID入仓。

后续拥有匹配 SDK 的环境先导入 `apps/harmonyos` 到 DevEco Studio，按
实际 SDK/配套 Hvigor 版本核对 product/modelVersion。用该环境已提供
的 ohpm/hvigorw 完成依赖解析、CompileArkTS/资源检查、unsigned
assembleHap；保留完整编译日志与产物SHA。构建缺失不要自动下载不明
SDK或全局改 npm。签名/真机安装另由 Root 在本人明确设备授权后处理。
本片不提供假 `hvigorw`、SDK类型stub或“编译成功”空产物。

## 权限与真机验收

本片仅声明 `ohos.permission.INTERNET`；FilePicker调用必须在UIAbility，
需要系统能力且其URI授权是临时的。权限UI管理本片共享接口，而非
限制其他App/原生agent。Preferences不是加密凭据库，本片不把token
放进去；本机私有成果仍含文件内容，用户主动分享前不自动上传。

真机下一验收：原授权enrollment及scoped join→原peer身份→一个原nonce
字节挑战→独立oracle/原registration→原Leader获取完整结果，再由本人
选择一份非敏感文件并运行受限能力；切后台/锁屏/网络切换/强杀恢复
保留unknown与原continuation，同项目第二任务不重建身份。当前均未做。
不是靠无障碍、shell、系统小艺权限绕开手机安全机制。

详见 [官方接口核对与适配边界](RESEARCH.md)。
