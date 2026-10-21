# 单独授权：agent 推理与生命周期薄入口

原生开发 Goal 保持 paused；评教须本人明确恢复才继续。本人此次仅授权
确认并补齐：网络/Leader 可为受管 agent（含自己）选择实际模型和推理
强度，与创建、配置、状态、停止进程在同一个简单入口中使用，允许受控
测试。不借此恢复 Chat/Events、学校业务或其他暂停工作，不另建协调任务。

现状核对：生产原3198适配器与此前主线只传模型，未传 turn/start.effort，
协作模式 settings.reasoning_effort 也未设置；不能把默认值/null称作Max。
本机已有 codex-leader-control 具备进程创建/续接/停止，但接口未暴露推理
参数；它不自动等于网内所有既有 agent 的完整控制面。

## 最短合同

- 一处入口提供 spawn / status / configure / resume / stop，创建和配置
  带 model、effort；既有 Leader/Claw 使用固定身份绑定的 owner-private
  profile，不从模型输入猜本机文件路径或授权。子进程不是新的共识成员。
- 按实际 native model/list 校验模型支持的等级；拒绝不支持的选择，不
  自动降级或购买 API 兜底。原生功能保持，已有认证仅引用、不复制。
- configure 明确下一次受管显式 turn/start 生效；不重启当前业务、不改
  历史、不把提示词当参数。原生 active-goal 自动轮未经过此调用时，不
  宣称已覆盖；支持范围/未决状态如实返回。
- 创建/续接保留原 request 身份和 durable intent；unknown 只查原状态。
  stop 只作用于确切 owned supervisor/Popen，不用裸 PID 杀主 Leader；
  停进程不等于删除会话。既有外部 agent 停止需其实际 authority 适配，
  未接适配就明确 externally_managed，不伪造成功。
- prepared/catalog checked、native accepted、原生运行记录匹配与实际
  完成分开。只有对应 thread/turn 的 native turn_context 可证明采用
  指定 model/effort；不以自报、配置文件或进程存在替代。

## 分工和有限验收

Root 为唯一协调/生产 writer；reasoning 叶在
`work/agent-reasoning-wire-20261020`（base ee38fe2），生命周期叶在
`work/agent-lifecycle-entry-20261020`（同 base）。旧额度/Events 分支保持暂停。
reasoning 叶已合入33ded33，Root5项检查0.006s通过；不冒称生产生效。

下一：取得实际 native 支持目录；有限独立两轮改变等级并核原生元数据、
原 session 连续性和进程回收；再核 Leader 同原线程下一显式轮次。拒绝
不支持等级应不启动推理。仅做必要小组检查，未有证据的环境明确待验。
上述为准备阶段；下列实际部署/验收覆盖其“尚未完成”的状态。

## 实际部署与原生采用证据

已集成 reasoning 33ded33、manager 78e5010/ebd38e2，以及并发 resume
修复684830c。独审真实复现“两条不同resume争同一completed session”后，
第二事务改为同时核原request/session/completed/revision，防止双启动。
Root 5项reasoning、4项manager及新增并发1项通过；两次打错新测试方法名
只产生loader错误，随后按实际方法名检查通过，不冒充产品代码失败/通过。

VPS 安装独立 `mesh-agent` 薄CLI与两份纯新增模块；namespace只补查原
冻结核心/既有入口模块，不用现代核心覆盖旧版本。宿主Python3.6.8实际
导入通过，精确native0.159.2的 `exec resume --help`确认所用参数支持。
原目录中的Worker/Codex/Store/receiver代码未换；原receiver PID不变。
在真实零running边界备份unit/config后，仅给原Leader/Claw加profiled
launcher，并显式 `Worker(..., backend=ProfiledCodex)`（不能只替换模块
变量，旧默认参数已在类定义时绑定）。两服务受控重启一次，随后active/
NRestarts0。原两session state及完整native文件的SHA在切换前后逐一相同，
原账本inode保持；未恢复DB/删除历史/重排旧输入。

真实native目录：Sol支持low/medium/high/xhigh/max/ultra，默认low；Luna
支持low/medium/high/xhigh/max，默认medium。这是本次账号/版本的实际
目录快照，不是永久模型白名单或对其他provider的推断。

有限实测已读真实原生元数据，不靠模型自报：

| 对象 | 同原会话内实际采用 | 原生记录时间（北京时间） |
| --- | --- | --- |
| 独立Luna受管测试agent | low → configure → resume max；两轮自然完成，原session不变，进程回收 | 10月21日00:29、00:33 |
| 原连续Leader | 本轮high，自己执行configure登记next=max；下一次显式轮真实max | 10月21日04:10、13:27 |
| 原Claw入口 | 原线程后续轮真实Luna max | 10月21日04:12、13:27 |

对应turn_context中model、顶层effort和collaboration settings的effort
逐一一致，task/thread/turn与原账本相符。Leader首个配置任务仅Root单次
提交，原请求最终epoch200完成，旧account_read前置失败记录保留；不把
epoch计数说成200次模型推理，也没换ID重投。后续确认任务epoch1完成。
CLI的 `effective_verified=false` 仍是保守的配置/执行回执；本节是Root
另外读取原生记录得到的验收，不把所有历史配置一律改成verified。

## 当前使用和边界

VPS既有Leader/Claw已获得同一入口说明；配置只指向原角色，下一显式轮
生效。新受管子agent可spawn/status/configure/resume/stop，停止保留历史。
真实两轮模型进程已自然回收；主动取消/迟到同组子进程回收目前以实际
OS合成supervisor检查证明，不冒称又做了第三次模型取消试验。

这不是无条件操控任何外部agent：旧外部Leader/Claw的stop仍明确返回
externally_managed，须走其既有authority；本机既有项目worker/其他平台
还未安装同一profile适配，不因此声明“全网任意agent全生命周期已完成”。
原生active-goal自动轮未经过显式turn/start时同样不在本次验收内。网络
仍可用原生SSH等方式部署/调用薄入口，不阻断任何原生功能。

本人要求经Claw联系：10月21日进度通知已单次入原通道并accepted，未
独立确认手机收件。整体开发Goal仍paused，评教独立暂停；Chat/Events/
Cloud/鸿蒙等旧实施未因此恢复，原43项协调任务及依赖保持。
