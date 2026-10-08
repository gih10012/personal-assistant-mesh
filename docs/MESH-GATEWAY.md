# 模型的 Mesh 能力入口

`mesh(action, arguments)` 是原生工具之外的附加工具。它不拦截、替换或禁用 Shell、文件、网络、MCP 或 runtime 的其他功能，不要求本机工具登记。受管调用被拒绝或连接失败时，agent 仍可在本人授权范围内使用原生能力排障、联网与尝试其他重并网路径。

| action | 受管入口行为 |
| --- | --- |
| `discover` / `describe` / `graph` / `audit` | 开放能力目录与证据查询；可用/声明不等于执行完成 |
| `advertise` / `observe` / `renew` / `link` / `revoke` | 现有资源 API 的登记、指标、关系和生命周期 |
| `request_grant` / `authorize` | 请求或核对 exact scope/action 授权；模型不能自行批准 |
| `remote_delegate` | 向已授权 peer 派发持久子任务；不是 SSH 或任意 URL 代理 |
| `delegate` / `children` / `wait_children` | 原有父子任务、等待及同一父会话续接 |
| `remember` / `recall` / `notify` | 原记忆/通知合同；通知 queued/accepted 不等于手机已收到 |

例如：

```json
{"action":"discover","arguments":{"kind":"compute"}}
{"action":"remote_delegate","arguments":{"peer":"cloud","input":"分析项目中的测试失败","project_id":"example","agent_id":"test-specialist"}}
```

资源类型和指标描述保持开放；未知资源动作通过现有资源 API 返回结构化错误，不会因为填写能力描述而自动执行。当前没有通用任意 capability adapter，也没有最佳路径自动调度器。`authorize.allowed=true`、目录条目和 `queued` 分别是授权、公告和任务状态，不能冒充完成。

身份来自当前认证 Client，父任务和 lease 来自当前运行环境，调用参数不能自行更换 actor/task/lease。资源 epoch 仍用于资源自身的版本核对。A2A 与 task lease 冲突继续终止该受管任务提交；这不是整台机器的原生权限门禁。

新的 Codex thread 获得统一名称和全部旧 `mesh_*` 名称。既有连续 thread 保留原有 dynamicTools，不为了加名字而新建会话或替换历史；其旧工具和私有 CLI/reference 接线继续可用。Pi 的支持与实测以扩展及 [验收记录](ACCEPTANCE.md) 为准，不由 Codex 工具定义推断。

私人认证、工具 payload、聊天和原生会话不进入公仓。入口参数不携带原始账号 token，模型生成的新工具无需白名单。
