# 现有本机能力适配；新节点自动化后续

本人此次明确：先把新节点自动化列plan，当前先适配已有电脑。最新
新建原生开发Goal已active；评教独立paused，不改参考项目或重放unknown。

## 当前结果

源码48e2e07/7ec7c6d已独审并合入，五项有限unit由原作者/独审通过；
真实包12个source pin、11个portable文件已私有准备并传VPS。首次安装
在中间目录权限775处停下，无完成receipt，Root保留原部分目标/原SHA；
定位为SSH umask0002与Path.mkdir(parents=True)中间模式差异，精确恢复
修复4a05981待审合/部署，不换目录或覆盖凑成功。尚未注册/实际使用。
Root完整读过
本机ncut-web-api、xiumi-native-clipboard与niri-computer-use的SKILL；
实际技能包/配置/认证和用户成果均留私有位置，不收入公开仓库。

| 当前代表能力 | 可迁移部分 | 应保留或独立配置的依赖 |
| --- | --- | --- |
| 校园skill | 说明、接口契约、无凭据的代码/检索逻辑 | 校园路径、本人登录态、Edge会话；不能拷cookie冒充目标已认证 |
| 秀米剪贴板 | 文章JSON组件生成、格式校验等逻辑 | 本机剪贴板、浏览器/秀米身份、图片上传本地化；生成与粘贴/保存分开 |
| niri computer-use | 调用约定/代理说明可以共享 | 真正GUI操作依赖提供节点的Wayland/DBus/显示会话与本机权限 |

粒度是动作与依赖，而不是把整套skill贴成“可迁移”或“不可迁移”。
可迁移代码在有依赖的目标执行；设备绑定动作通过现有认证A2A交原节点
辅助agent。Agent仍可使用全部原生Shell/网络/MCP；这只是新增受管入口。

## 最小交付

1. 现有电脑三类能力的owner-pinned inventory/manifest，记录class、实例、
   provider、动作、版本、依赖、设备绑定及证据层级，不导出secret值。
2. 便携包只含已审代码/必要说明与refs；目标适配路径和依赖，不复制账号
   state、令牌、图片、私有结果或本机任意目录。
3. 复用现有能力目录和remote_delegate/A2A，为niri提供本机helper路径。
   代码存在、插件enabled或请求accepted不是实际执行证据。
4. Root串行部署。GUI保持单操作者，遵守原熄屏/最低亮度/恢复约定；
   真实A2A/只读窗口枚举交Leader周验收队列，不执行学校业务或修改应用。

独立专员existing_node_capability_adapt_20261021，branch
work/existing-node-capabilities-20261021，base423a8a6；Root是唯一共享
协调/生产/GUI writer。开发、安装、声明与实际可用分别记录，不为了交接
强行恢复原Leader主scope中的未知转达任务。

## 明确排到后面的新节点自动化

加入后先盘点已有skill/MCP/plugin与实际环境；Leader或下属适配agent
判断直接复用、迁移纯逻辑、补目标依赖或A2A回原节点。再有限核可用性、
权限和结果，登记为Mesh能力并保留更新/失效信息。环境中立，不强迫
Windows/手机照搬Linux例子，也不把程序的静态猜测当作AI已完成适配。

该流程复用PAM-005，当前仅列计划；不启动通用自动安装、全网扫描或
所有skill的批量迁移。
