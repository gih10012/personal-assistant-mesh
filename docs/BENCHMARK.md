# Dots 对标与能力边界

对标对象是官方 Dots 的长任务、连续上下文和主动协作，不是 OpenClaw 工具数量。
产品层实际 fork 自 composio-community/open-dot 的
`f838e17cf5c3a88ade5ceea54680a8145d048c1d`，不是官方源码。
上游快照无 LICENSE，不重新标为 MIT、不复制进本仓库。

| 能力 | 现有证据与缺口 |
| --- | --- |
| 用户离线后继续工作 | 云端独立入口和持久账本已部署；云端模型执行/失效接管须单独验收 |
| 连续个人 agent | 新任务与 worker 重启后准确回忆 native 前文，已实测 |
| 子 agent 协作 | 实际委派、父任务等待/唤醒；同项目/身份连续性有事务测试 |
| 记忆/压缩 | 原生 thread/rollout，无应用自写摘要；native memories 后台生成仍需观察 |
| 边做边聊天 | native steer/持久回答接线；实时 UI 交互需另验收 |
| 计划/目标 | 真正 collaborationMode/thread goal RPC，协议测试覆盖；自然语言计划不能冒充 native plan |
| 自主环境/工具 | 原生全功能 runtime 实际写入/读回已验证；完整工具技能生命周期仍未完成 |
| 多端资源组合 | lease/fence 与 artifact 基础；质量/性能/路径图正在扩展 |
| 浏览器/应用 | native skills/MCP 可发现；上游浏览器/Composio UI 不自动等于 native 接通 |
| 主动通知 | 防重、未知不重试已测试；新部署手机确认和跨夜稳定性未闭环 |

官方依据：[Tasks and memory](https://learn.chatgpt.com/docs/dots/tasks-and-memory)、
[Codex App Server](https://learn.chatgpt.com/docs/app-server)。这些说明产品/协议，不替代本项目运行证据。

保持先进性：实时模型目录、原生长上下文和工具环境、模型自主发现/生成/测试/改进工具，
以可重复证据评价改进，不固定工作流、不贴“前沿”标签冒充实现。面板观察成果，不限定 AI 只能点击已有按钮。
