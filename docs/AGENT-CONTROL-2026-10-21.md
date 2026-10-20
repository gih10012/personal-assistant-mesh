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
当前仍未部署新入口，也未完成真实模型或 Leader 验收。
