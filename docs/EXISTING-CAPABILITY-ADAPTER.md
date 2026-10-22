# 现有电脑的 skill / MCP 薄适配

对应本人 2026-10-21 的本机授权、PAM-004d / PAM-005；最新新建开发
Goal已active，评教仍独立paused。本片不办理评教。新节点自动盘点、自动迁移留在后续计划。当前只提供现有
三例的显式适配；不新增网络、任务库、daemon，也不改原 Worker / Store。
实际部署记录由 Root 填入 [当前小片](LOCAL-CAPABILITIES-2026-10-21.md)。

## 动作，而非整套 skill，决定执行位置

| 实例后缀 | Class / action | 默认执行位置与必要条件 |
| --- | --- | --- |
| `ncut.knowledge` | `knowledge.lookup` | 可迁移原说明、代码、选定契约；Python ≥3.9，本地检索不登录学校 |
| `ncut.business` | `campus.business` | 原校园节点；账号、出口和业务授权分别核对。目标也可独立取得依赖再适配，但不能继承原 cookie |
| `xiumi.compose` | `document.compose` | 可迁移组件构造原脚本与 format，Python ≥3.9；输入图片需另有合法可读来源 |
| `xiumi.validate` | `document.validate` | 可迁移原 JSON 校验逻辑；校验通过不等于已上传、粘贴或保存 |
| `xiumi.localize` | `images.localize-and-paste` | 持有本人浏览器、剪贴板、图片授权的节点；按原 skill 两段真实操作 |
| `niri.observe` | `desktop.windows.read` | 原 niri/Wayland/DBus 桌面节点；静态 enabled / 文件存在不是 backend 可用 |
| `niri.control` | `desktop.operate` | 同上且需当前业务授权、GUI 单操作者；不在 VPS 假装获得本机桌面 |

`provider:后缀` 是实例 ID，同 class 可有多个实例。`source` 是技能来源，
`source_provider` 保留代码原节点；设备动作另携 `source_device/proxy_binding`。
代码、执行依赖、认证引用、设备绑定分开记录，不把迁移代码等同迁移身份。

此次发现的真实依赖差异：原 `ncut.py` 顶层使用 `zoneinfo`、
`Path.is_relative_to`；秀米使用 `from __future__ import annotations` 等
现代 Python 功能。**不能把原文件复制到 VPS Python3.6 就称可运行。**
可复用已有隔离现代 Python 环境，不升级宿主或创建新的账号/模型 key。
Linux 0700/0600 是本 CLI 的存储适配；合同不要求未来 Windows/手机照搬。
秀米原 `serve` 还依赖原项目 `index.html/schema`；本包有意只搬纯组件/
校验所需代码，不把缺少前端工程的目标标成可运行浏览器交付服务。
`serve`、剪贴板和账号本地化继续委派原来安装完整工程的电脑。

## 来源清单与私有便携包

`python3 -m assistant_mesh.existing_capabilities` 可独立安装在新的源码目录，
不需要加载新服务或原核心。所有命令只读源码/生成私有文件和现有 API
负载，不执行技能、调用模型、访问业务 HTTP、修改原 profile 或注册能力。

先在仓库外本人 0700 目录保存 0600 选择清单，参照
[选择格式](../deploy/existing-capabilities-selection.json.example)。示例绝对
root 必须换成现有 symlink 解析后的真实目录；逐个列明本人已审的文件，
没有递归通配符。`pin` 再固定每个文件的完整 SHA。最多 160 文件、单文件
1MiB、总源码 8MiB。`SKILL.md/scripts/references/agents` 以外文件、账号/
state、链接、并发漂移和明显认证文件名被拒绝。

示例只有校园知识查询的最小代码入口和一项契约，**不是全量校园知识库**。
所选说明仍可能含本人业务信息；源码审查及逐文件授权是必要条件，哈希
和文件名检查不是完美 DLP。若要完整校园工作方法，应明确加入所需脚本/
refs；不把整个 `~/.codex`、MCP 配置、state、结果、图片或 native history
打包。现有 skill 原文不改写，不新造另一个不一致的 skill。

```sh
python3 -m assistant_mesh.existing_capabilities pin --selection /PRIVATE/selection.json --output /PRIVATE/owner-pins.json
python3 -m assistant_mesh.existing_capabilities inspect --owner /PRIVATE/owner-pins.json --owner-sha256 EXACT_OWNER_FILE_SHA
python3 -m assistant_mesh.existing_capabilities bundle --owner /PRIVATE/owner-pins.json --owner-sha256 EXACT_OWNER_FILE_SHA --output /PRIVATE/skill-bundle.json
```

`pin` 输出原 owner 文件 SHA；`bundle` 输出包 SHA。`inspect` 读全并校验
每个已选源文件，但只回传 descriptor/数量，不泄漏源码绝对路径或私有
认证引用。依赖状态仅 `missing/configured_not_verified`，从不自签 live。
改变来源先更新 revision / pins，并按原 registry epoch CAS 明确更新；
旧 unknown / operation 不因此重新执行。

Root 用已授权 SSH / 现有文件传输将该 **0600 私有包** 放到目标私有目录。
包只含精确源码及必要 refs、hash 和来源节点，无 source 绝对路径、依赖
auth 句柄或 niri backend；传输并不授予业务、GUI 或账号使用权。

```sh
python3 -m assistant_mesh.existing_capabilities install --bundle /PRIVATE/skill-bundle.json --bundle-sha256 EXACT_BUNDLE_SHA --destination /PRIVATE/NEW-SKILL-CODE
```

目标必须是不存在的新目录；文件 0600、目录 0700，不覆盖既有安装。
`installation.json` 保留完整来源/hash；中途失败保留部分目录，不假装已
激活或自动换目录重试。目录逐层以0700创建，不借全局umask或放宽检查
掩盖 `Path.mkdir(parents=True)` 的中间目录权限差异。此步不改 `CODEX_HOME`、原 skill、MCP、插件开关
或认证。Root/本机适配 agent 选择适合目标 profile 的原生安装方式，再
单独检查依赖与真实使用；目标无现代 Python 时报告缺项，不伪造可用。

若原安装只写入部分成员，Root先保留原目标/原包SHA并检查原失败事实。
`install --resume-exact` 是显式续接，不是自动重试：同一命令继续使用
原 `--bundle/--bundle-sha256/--destination`，只补缺失成员及最后receipt。
写入前完整遍历原目录，拒绝任何额外路径、symlink/hardlink、不同字节、
非0700目录或非0600文件。旧错误创建的775目录由Root仅对已独立识别的
确切路径显式收紧0700；安装器不会自动改权限、覆盖或删文件。已有匹配
receipt要求所有成员完整匹配，随后只读返回；缺成员的早到receipt也拒绝。
若原写入留下半截文件，保留并停下另行审核，不覆盖半截文件凑成功。

## 接回既有能力目录与 A2A

生成 Registry 原格式负载，不换用另一个目录：

```sh
python3 -m assistant_mesh.existing_capabilities advertisements --owner /PRIVATE/owner-pins.json --owner-sha256 EXACT_OWNER_FILE_SHA
python3 -m assistant_mesh.existing_capabilities advertisements --owner /PRIVATE/owner-pins.json --owner-sha256 EXACT_OWNER_FILE_SHA --execution-provider cloud
```

前者返回现有电脑的七个声明，后者只返回三个 portable 动作，不在 VPS
登记 niri / 现有校园登录 / 本机剪贴板。`execution-provider` 不是身份
覆盖：实际提交还由原 `/v1/resource/action` 的认证主体决定，不能
为另一主体注册授权。Root 在实际安装/必要检查后显式提交原负载；旧
目录联邦自动转发声明，不转发 grant / 容量 / 认证。原 Registry 的
`available` 仅说明租约/声明健康，不是实际调用验收。所有新 spec 都保留
`source_pinned_not_executed/runtime_verified=false/managed_use_authorized=false`。
真实结果通过原 owner 独立观测保存，不能把改这个 boolean 当作核验。

设备动作继续用现有 `remote_delegate`。Leader 持有真实当前任务时生成：

```sh
python3 -m assistant_mesh.existing_capabilities delegate-request --owner /PRIVATE/owner-pins.json --owner-sha256 EXACT_OWNER_FILE_SHA --task-id ORIGINAL_TASK --epoch 1 --operation-id ORIGINAL_OPERATION --action-id niri.observe --arguments-file /PRIVATE/arguments.json
```

返回值直接用于原 `/v1/mesh/delegate`；或把其中 peer / arguments 交给
原 `mesh(action="remote_delegate", arguments={peer,input,project_id,agent_id,role})`。
旧线程对应原 `mesh_remote_delegate`。**当前本地电脑与 VPS 是不同
authority**；VPS 使用已认证 peer `laptop`，电脑辅助 agent 以自己的
实际 profile 读取原技能、调用本机 MCP，返回原 A2A receipt/result。
没有本机 backend 就报具体缺项，不把父端工具表或 niri 安装到 VPS 来
冒充能力。已可直接在目标执行的纯代码无需绕 A2A。

生成器不创建任务、scope 或第二执行库。`call_id` 由原 task/operation
稳定派生，原 `Remote` 在同一事务检查父任务/Leader 活租约，建立原
child/delivery/回执；同 ID 同内容不会双派，改内容/过期 epoch 拒绝。
异常/unknown 后只读原 child/回执，不重新生成不同输入/epoch 或换 ID。
源码 template/prompt 不是对恶意同 UID 进程的沙箱，也不是物理执行证明。

## 本机辅助 agent 执行约定

1. 先读当前独立授权、原 skill 与 source pins，检查实际 profile / MCP
   backend/依赖；代码存在、enabled、排队或 accepted 均不等于成功。
2. 校园登录、浏览器和业务均在原节点；本片不办理学校业务、不恢复评教。
   秀米 JSON 生成/校验与本机图片本地化/粘贴/保存分开。不要导出 cookie。
3. GUI 先协调唯一操作者，有其它操作时返回 busy，不抢屏。按原 niri
   SKILL：输入前 begin；只在需输入时 wake，入口熄屏则最低非零亮度；
   所有退出路径 end / status 恢复原电源和亮度。只读窗口枚举不唤醒。
   只关闭自己打开且已无用的临时窗口，保留用户窗口和正在工作的终端。
4. 返回真实动作、时间、原操作/任务/回执及必要结果；窗口标题/截图等
   私有数据仅在原授权范围返回。读窗口不变成任意点击或学校业务授权。
5. 效果未知保持原身份，不重派；Mesh 仅增加共享路径，所有原生 Shell/
   文件/网络/MCP 功能保持，不因目录失联禁止节点自主排障。

## 验证范围

五项针对性离线检查：动作拆分与原 Registry 兼容、不带认证/设备 backend
的精确源码包、新目录/权限/无覆盖、漂移/链接/越界拒绝，以及原 Remote
事务同 ID 防双派/内容与 epoch fence。均为合成文件和临时账本，不表示
已部署、已读本人登录、真实模型/GUI 或全网自动迁移完成。

Root以当前三例做适配/安装与声明；真实只读A2A窗口枚举交Leader统一
周验收队列，现在不为安装补跑GUI/模型。该动作的验收不外推所有
computer-use、校园业务或秀米账号保存。最新开发Goal已active，但
评教及其它明确暂停业务不自动恢复，原unknown仍保留。
