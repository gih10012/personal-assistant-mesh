# 既有设备的原生能力入口

负责人 `/root/final_executor_membership_20261022`；独立分支
`work/existing-capability-discovery-20261022`，worktree同名，base
`f64e5c283fd47bbec8023dcd138c175973e20564`。D3：节点发现、原权限与旧
Registry兼容；沿继承强路线续接，额度不足保留检查点等待，不免费降级。
Root是唯一生产/profile/注册writer。本片没有SSH、真实profile改动、
认证、GUI、模型或业务API实测；评教保持独立paused。
10月23日按Root确认的额度恢复指令沿同一分支/检查点续接，没有重建
已完成final片、换低质量路线或重跑已完成业务。

## 真实agent接下来得到什么

`deploy/mesh-existing-capabilities` 是一份窄的、可自动发现的Mesh skill。
它读取生成的私有binding，指向已经安装的原ncut知识子集和Xiumi
compose/validate代码，而不是复制/改写原skill。自带的 `scripts/verify.py`
先检查原receipt、每个source pin和Python3.9最低版本；它不import原代码、
不登录、不发业务请求。验证成功仍明确 `runtime_verified=false`。

入口让agent用原脚本/模块执行三项portable动作；学校认证/网络、Xiumi
serve/图片本地化/粘贴/保存及niri始终经原 `remote_delegate` 交原provider
本机helper。源peer来自原bundle，不是自由目的地址。原helper读取自己的
原skill和当前MCP；niri只复制说明不会产生backend。已有A2A task/epoch/
Leader lease、原child/delivery/unknown回执不换身份。

源bundle SHA仍是原 `91600fce20691488bb783a52cf216550695c09c56c0d68be97e158a94bc68ae5`，
原11文件安装目标不改。新入口有独立新目录及receipt，只新增指向它的
精确skill symlink，不覆盖别的skill或原config.toml，不拷认证/native历史。
运行时输出和业务文件留在原授权工作区，不写回source安装目录。

## Root最短串行apply

先把本次helper、模板两文件和原包置于目标的私有源码/准备目录。复用
既有安装器依赖；不复制现代Store/Worker，不重建MCP/App/OAuth。原
`Registry.advertise` 仅增加可选 `health`，原schema不变。Root在原authority
加载这项兼容变动后才提交新注册负载；原Store/Worker/final hook无需改变。
`deploy/existing-capability-entry-source.sha256` 固定本片五个源码成员，
不是整个receiver/runtime manifest。原resources源码SHA为
`a932355b508b95c599a07d819f54a1fd9a889637d12a177581d6a05bc84ecade`，
本片为 `00480f7819703b01e833a66a5f7e98b2bc08539cf077b1fef0d2db33ab0419ce`；
它在base与旧3198逐字节一致，本片仅加初始health兼容参数与同INSERT赋值。
Root需核实际在用源码，不能据此假定一个后来改过的私有副本也匹配。

下面全是参数形状，实际目标/profile/Python由Root核当前launcher后填写。
原source安装路径已知，但本机适配专员没有远程探测其它实际路径。

```sh
python3 -B -m assistant_mesh.existing_capabilities installed-discovery \
  --bundle /PRIVATE/original-bundle.json --bundle-sha256 ORIGINAL_BUNDLE_SHA \
  --installation /PRIVATE/ORIGINAL-INSTALL --execution-provider cloud

python3 -B -m assistant_mesh.existing_capabilities entry-plan \
  --bundle /PRIVATE/original-bundle.json --bundle-sha256 ORIGINAL_BUNDLE_SHA \
  --installation /PRIVATE/ORIGINAL-INSTALL --execution-provider cloud \
  --destination /PRIVATE/NEW-ENTRY --python-executable /PRIVATE/EXISTING-MODERN-PYTHON \
  --profile-skills /PRIVATE/ACTUAL-PROFILE-SKILLS --output /PRIVATE/entry-plan.json

python3 -B -m assistant_mesh.existing_capabilities entry-install \
  --plan /PRIVATE/entry-plan.json --plan-sha256 EXACT_PLAN_SHA
```

`installed-discovery` 复核完整原receipt/全成员/权限与pins，报告当前检查
进程能否编译代码及Python版本；不把该检查进程冒称选定运行环境。
选定Python/已有透明launcher必须能在当前native agent文件系统内访问
原安装路径，实际运行须≥3.9；host3.6不能运行原skill。可复用既有隔离
环境，但不因本片新建账号、付费环境或隐式安装依赖。使用launcher前按
原AGENTS先核其代码/元数据；installer只stat、不执行它。

`entry-plan` 只生成0700父目录中的0600私有负载及SHA，不写profile。
负载可审阅：三个入口文件、原安装binding、唯一link路径，以及三条原
Registry advertise请求。模板位置不是标准源码布局时显式给
`--template-root /PRIVATE/mesh-existing-capabilities`；目录内必须是本次
`SKILL.md` 和 `scripts/verify.py`，两者字节进入plan/receipt pins。

`entry-install` 默认create-only。只有原目标已存在的明确续接使用同一
plan/SHA加 `--resume-exact`：先复核原source、整个部分入口树、已有link；
只补缺失成员。不同字节、额外路径、链接或非私有权限都拒绝，无覆盖/
chmod/删除/自动换目标。完成后才创建唯一skill symlink；已有同目标link
只读保留，不同目标/同名真实目录拒绝。失败保留原部分状态，不自动retry。

Root需选当前真实profile使用的**物理**skills root，不能从本次控制会话
的工具表推断目标Worker具备相同skill/MCP。仓库Codex适配会为每个child
选择其auth_home/CODEX_HOME；不要盲目给另一个账号目录安装。同一入口
可由Root对另一明确profile另生成相同文件的新plan并显式resume添加其link。

当前官方文档说明本地skill支持symlink，app-server可用 `skills/list`
按cwd查实际可见项；新字段/扫描路径仍须与目标已装版本一致。
[本地skill发现](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)、
[skills/list](https://learn.chatgpt.com/docs/app-server#skills)。Root在原当前
profile/已有app-server查询 `skills/list(cwds=[ACTUAL_CWD], forceReload=true)`，
核到本入口的真实path/enabled后才记录“原生已发现”。不改extra roots来
伪造已安装发现，也不启动模型来证明文件存在。niri实际使用前由laptop
helper核当前thread配置的MCP目录；读取目录不等于已执行GUI。

## 注册、可用性与实证

apply返回 `registration_requests`，仅含三个portable动作，元数据沿原
class/instance/source SHA与provider；无source绝对路径、认证引用、niri
backend、grant、pool或managed-provider binding。Root用原目标worker认证
提交原 `/v1/resource/action`，不能用operator冒充 `node:cloud`。首次注册
同次INSERT设置 `health=unavailable`，所以默认本地/联邦目录不会产生
“先available再补不可用”的窗口。`include_unavailable` 可读原声明/缺项。

完全相同且租约仍有效的advertise重查幂等、不续租；已有不同内容/过期
记录仍须先读原epoch再显式CAS，不猜epoch、不换实例ID。可选health省略
时仍是原unknown默认。旧authority不支持health会直接拒绝参数，不得
删除该字段后重投。health也是provider声明，不是独立执行证明。

原生入口可在真实owner授权任务内使用原代码，Mesh目录不是原生工具的
白名单。真实结果、当前profile可见性、独立观测与业务完成分别保存。
之后沿原Registry observe/renew/epoch机制记录实证、显式更新health，
不能改一个boolean冒充验证；未增加另一个状态机、任务库或常驻循环。
普通集成/模型/GUI/A2A验收仍排Leader周队列，不重放旧unknown或评教。

## 有限本地证据

Python3.14本地3个新增unit和2个必要旧兼容检查共5项/0.057s通过，零skip。
覆盖原source-only广告不进入默认可用目录、新初始health同INSERT（错误
初始值会由真实SQLite trigger拒绝）、旧unknown默认和幂等期限不变。
新入口检查实际plan/CLI安装/精确resume/link，以及source漂移或已有异目标
在写入前拒绝。合成原脚本在import时会抛错，独立verifier仍只读核pins，
不执行原技能。后续仅复跑改变过的入口边界，最终2项/0.048s通过。

Skill Creator的quick_validate使用既有隔离环境通过；第一次系统Python
缺PyYAML，仅是验证器依赖缺项，没有新增安装/放宽检查。四个Python源码
成员通过3.6 grammar解析，不冒称宿主3.6实跑或原技能能在3.6运行。
官方文档用于确认原生skill/symlink/skills-list接线；Skill Creator用于
保持入口窄作用域、引用原skill并验证格式。未修改原bundle或私有审阅内容，
没有真实native发现、业务执行、平台或GUI验收结论。
