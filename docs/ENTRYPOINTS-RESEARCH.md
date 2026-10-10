# Chat / 原生 Codex / Codex Cloud 接入研究

初版核对日期：2026-10-09；实际入口补充：2026-10-10。本文区分官方
已支持的接口、项目设计推论、以及尚未验收的账号能力。初版研究只读
文档、公开仓库和本项目代码；后续 Root 的实际核验/安装另列如下，
没有将研究当接通、创建 Cloud job 或购买 API 服务。
优先顺序是底层接入和能力交换，Live 不在本轮实现范围。

## 三种入口不是同一种运行环境

| Surface | 可作为入口 | 可作为 Mesh 工作节点 | 当前应采用的方式 |
| --- | --- | --- | --- |
| ChatGPT Chat | 普通 Chat 的自定义 MCP 能力须实际账号核验；Firefox 手动桥接是独立入口，不是模型原生工具 | 支持的对话/定时 run 可研究、选择工具、回传结果；不是常驻本机进程 | 先实测普通 Chat；官方 quickstart 的明确验收面是 Work，不能外推 |
| 原生 Codex | 原生聊天和终端均可提交/查询 Mesh 工作 | 可以在本人机器常驻或自主离线工作，保留原生 Shell/记忆 | 现有 app-server、持久 thread、Mesh 附加工具 |
| Codex Cloud | 官方 CLI 可发起并观察 cloud task | 每个 hosted task 可作为有租约的临时 agent；不要假定永久 daemon | 发布 cloud environment + 显式 cloud job adapter |

上表是基于接口的**设计推论**，不是三种入口已经上线的声明。官方
自定义 MCP 接入支持读/写 tools，受账号、workspace 安全策略和确认
设置约束；它不承诺本人的 Plus 账号已开放全部配置按钮。
[自定义 MCP 接入](https://developers.openai.com/api/docs/guides/custom-mcp-server)

后续实测补充（2026-10-10）：owner-terminal 已实际提交、原 Leader 原线程
处理并回查审查后的受限摘要；不是 Chat 接入。Firefox 的账户设置已核对
本人指定主账号，Firefox 155 开发扩展已临时加载，专用 native host/
ingress peer 已安装。按授权进行人工确认后真实 browser submit 仅一次，accepted 回执明确
`task_created=true`；原稳定 request 状态回查也已通过。原生 history
restore 的旧前缀冲突曾令任务 `waiting_backend` epoch 5；epoch 15 完整
历史维护后原请求原 thread 已实际 completed epoch 16，Root 独立审查/
受限摘要发布及原 browser 批准成果回查全往返已通过；没有证明 Chat
模型原生调用 Mesh。
Cloud 原生只读 list 成功，官方 repo-scoped 环境查询 HTTP 200 返回
空数组；该查询未返回本项目环境，未提交 job/登记 Cloud node。见
[Firefox 合同](FIREFOX-CHAT-ENTRYPOINT.md)、[Cloud 实施](CODEX-CLOUD-JOBS.md)。
[Work quickstart](https://developers.openai.com/plugins/quickstart) 要求切换到
Work；普通 Chat 是否支持 MCP 不能只看插件按钮或参考账号。

Firefox 原 request
`mesh-firefox-05d0bce8d4728cd553d831b9317529e3` 保持不变，实际 task
`ingress-c3d8bbe11285b26aab751c96b9dd8fe19229aea0bdb78900f6becf44c8799b76`
仍使用原 Leader 上下文。维护前只读诊断确认目标 9160820 字节是权威完整
9964417 字节历史的严格旧前缀，不是分叉。历史观察中 epoch 5 restore 失败
的 `native_start_attempted=false` 不证明全 epochs 零效果，不授权换 ID
或清效果重做。Root 首轮 backup() 不兼容在 publish 前失败，worker 恢复
active、history 未改；改用一致 read-transaction SQL dump 备份后，
epoch 15 完整旧字节保留、严格前缀/终结 native/未变引用核验及同原路径
原子刷新已完成。维护前后 task/native authority 行一致、效果标记未改、
DB 未恢复，仅该 worker stop/start。原 task 随后在同原 thread 的 turn
`01a1217d-f3e4-7282-878b-5c4e44a15173` completed epoch 16。Root 独立
review 后发布 `PAM-006b-firefox-result-20261010-v1` 审查摘要，没有转发
原 native result；Firefox 真正查到 completed 并读回 approved result，
publication/task/request 精确匹配，手动全往返已验证。成果 execution/
artifact-content/account verified 均仍 false；这份成果是当时目录为空、
004b 仅 stage 的快照。之后 004b 已发布/激活安装绑定、配置 owner 容量
并取得新独立 probe，第一轮模型仍等待证据、未预留或执行。下一步继续实际出口，
不是据这一桥接声称原生 Chat tools/Cloud 节点或整体 goal 完成。

新增 Firefox/Cloud/Codex MCP 源码已以 `7fb001a` 公开发布，CI
[37960801403](https://github.com/gih10012/personal-assistant-mesh/actions/runs/37960801403)
实际 completed/success。本机完整 1240 项测试通过，包含
28 项 native-host、32 项 Cloud、32 项 MCP 测试及 19 个 JS 离线 cases；
正式核心服务仍是 cf308b3，两端/公开 CI 各 1148 项证据。VPS 隔离运行
7fb001a 的 1240 项时，两处测试使用了 Python 3.6 不支持的 subprocess
参数；修正为当前解释器/兼容参数后隔离 1240 项通过，不修改系统 Python。
原生 Codex 已在本机一个既有 profile 安装 stdio MCP，实际工具发现、
submit/status/result 调用及原 Leader completed/批准摘要读回均通过，详见
[MCP 验收](MCP-ENTRYPOINT.md)。验证客户端没有模型 turn，不把实际
客户端调用说成模型自主调用。不要用绿色测试替代 browser 全链路、官方 Chat connector
或 Cloud 入网证据。

原生 Codex 可用 `thread/resume` 继续实际保存的 thread；恢复不需要为了
添加入口而换成应用自写的摘要。动态 tools 和原生能力保持分离。
[App-server thread 合同](https://learn.chatgpt.com/docs/app-server#start-or-resume-a-thread)

Codex Cloud 的 published environment 是复用的准备环境，每个新 task
有独立 workspace；既有 task 保留自己的状态。不要把环境缓存等同于
跨 task 的同一个 Leader 会话，也不能把任务提交回执等同于 agent 已
上线。[Cloud 概览](https://learn.chatgpt.com/docs/cloud)，
[Cloud 环境](https://learn.chatgpt.com/docs/environments/cloud-environments)

## ChatGPT MCP：可复用开源代码，不复制认证漏洞

当前官方开发文档以 **Plugins** 组织此前 Apps SDK 的相关 MCP 接入。
最小 bridge 不需要自定义 UI：稳定 HTTPS `/mcp`、Streamable HTTP、
清晰 tool schemas、structured results 即可。展示面板可以后加。
[构建 MCP server](https://developers.openai.com/plugins/build/mcp-server)

已实际查看下列仓库的 README、许可证及指定源码；这些是候选组件，
不是替换 Mesh 或原生 Codex 的另一套 Leader：

| 候选 | 许可证及实际查看源码 | 可复用内容 / 必须补的边界 |
| --- | --- | --- |
| `modelcontextprotocol/python-sdk` | [MIT 仓库](https://github.com/modelcontextprotocol/python-sdk)，[resource server 实例](https://github.com/modelcontextprotocol/python-sdk/blob/main/examples/servers/simple-auth/mcp_simple_auth/server.py) | MCP transport、typed tools、OAuth resource metadata / token-verifier 接口。实例明确不是 production；`oauth_strict` 默认 false，不照搬默认值。当前 main 是 v2 风格 `MCPServer`，不能和旧 FastMCP 导入混用。 |
| `PrefectHQ/fastmcp` | [Apache-2.0 仓库](https://github.com/PrefectHQ/fastmcp)，[OAuthProxy 源码](https://github.com/PrefectHQ/fastmcp/blob/main/fastmcp_slim/fastmcp/server/auth/oauth_proxy/proxy.py) | 较完整的 OAuth/DCR/CIMD、PKCE、redirect 验证和持久加密 auth-state 组件；仍须自己的 owner 映射、scopes、审计与重启/撤销测试。不是部署后自动得到 Mesh 授权。 |
| `openai/openai-apps-sdk-examples` | [MIT 许可证](https://github.com/openai/openai-apps-sdk-examples/blob/main/LICENSE)，[authenticated_server_python/main.py](https://github.com/openai/openai-apps-sdk-examples/blob/main/authenticated_server_python/main.py) | 可借 securitySchemes、OAuth challenge、结果/可选 UI 形状。实际示例只是检查 bearer 是否存在，然后返回静态订单；没有生产级 token signature/issuer/audience/owner 校验，不能把这个 handler 直接暴露为本人 Mesh。 |

Python SDK 和 FastMCP 当前分支要求 Python 3.10+；应在独立现代运行环境
部署 sidecar，不改变已有 VPS Python 3.6 Mesh 服务。实际安装要固定
通过验证的 release/commit 和依赖，不 `pip install` 移动 main 后宣称
兼容。源码阅读不是完整第三方安全审计。
[Python SDK requirements](https://github.com/modelcontextprotocol/python-sdk#requirements)，
[FastMCP package metadata](https://github.com/PrefectHQ/fastmcp/blob/main/fastmcp_slim/pyproject.toml)

建议采用 FastMCP 的 auth 组件或官方 SDK 的 resource-server 接口，
业务 handler 仍保持薄层：验证身份/作用域 → 调用现有 Mesh 合同 → 返回
稳定 ID 和真实状态。协议库只负责 MCP；任务、Leader、效果账本仍由
本项目负责。不采用依赖旧 `codex mcp-server` 的包装作为原生运行器：
该命令已从当前官方 CLI 移除，替代接口是 app-server。
[Developer commands](https://learn.chatgpt.com/docs/developer-commands#codex-mcp-server)

## 推荐 bridge 合同（待实现）

以下 tool 名称是建议接口，不是声称已部署：

- `mesh_profile`：凭已验证的连接返回当前授权身份和非敏感范围，让本人
  能辨认账号。不能从提示里的 `@163`、email 或 actor 参数推断身份。
- `mesh_projects` / `mesh_task_result`：只读本人可见项目、任务状态和
  成果引用；不要默认返回整个私有 ledger、凭据、原始日志。
- `mesh_submit`：开放任务描述、project/agent 标识和稳定 request ID；
  原子记录 source subject、内容 fingerprint 与 task ID。重试只使用
  同一 ID，内容变更冲突。`accepted` 只证明收账，不证明做完。
- `mesh_steer`：向已授权任务追加方向，稳定 steering ID，接收/消费状态
  分开；不自动新建或替换当前 native Leader thread。
- `mesh_capabilities` / `mesh_report`：按真实范围发现开放能力，并记录
  临时 Chat/Cloud run 的结果；provider 自报不是独立验证。

MCP API 的有限 schema 约束入口身份和效果，**不是**约束模型可用的
原生 Shell/MCP/网络。不能借一条通用 `shell(command)` 公共 MCP 工具
把未经映射的网络调用直接变成本机任意执行；获授权的节点 agent 仍可
自行用它的全部原生能力完成开放任务。

最初研究时的项目缺口：[server.py](../assistant_mesh/server.py) 的顶层
`/v1/tasks`、control、steer 等写入仍是 operator route。不能把现有
worker credential 接上 MCP 就声称已支持本人入口，也不能把全权限
operator bearer 发给 ChatGPT 或塞入 connector。应先实现独立
owner-bound ingress principal 和作用域写入；内部账本复用，认证边界
不绕过。Chat/Cloud 临时 node 还要绑定实际 run ID、租约、任务范围，
失效后只能核对旧结果，不能继续冒充有权 Leader。

公开部署前必须有可验证的 HTTPS origin 和 OAuth 配置。ChatGPT 的
标准用户认证是 OAuth 2.1 authorization-code + PKCE，并有 resource
metadata、issuer/audience/expiry/scopes 检查。它不能直接提供任意
客户 API key 或 machine-to-machine client-credentials grant。
OpenAI-managed mTLS 可以辨认 MCP 客户端，但不是本人的身份认证。
[官方认证合同](https://developers.openai.com/plugins/build/auth)

每个写工具如实标记 `readOnlyHint=false`；annotations 和客户端的
记住批准不能代替服务端鉴权。官方接入说明写动作默认要确认；对话中
记住选择的范围和刷新/新会话后行为也有边界，不能承诺无人值守写操作
永不再问。[Tool confirmation](https://developers.openai.com/api/docs/guides/custom-mcp-server#how-to-use)

官方 Secure MCP Tunnel 不作为本轮“无需新 key”方案：它需要 Platform
tunnel 权限、workspace association 和 runtime API key。当前不新增
API key/消费，因此不配置该 tunnel；先完成 localhost bridge 和
OAuth/权限合同，再在已有本人 VPS 的合法 HTTPS origin 接入。
[Secure MCP Tunnel prerequisites](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels#before-you-start)

## Codex Cloud job lifecycle

官方 CLI 文档确认 `codex cloud exec` 和 `codex cloud list --json`；
提交使用 CLI 已有认证，失败 exit 非零。实际 environment ID 及命令
支持以当前客户端/账号为准，以下只是命令形状，**此次没有执行**：

```sh
codex cloud exec --env ACTUAL_PUBLISHED_ENVIRONMENT_ID --attempts 1 'Explicitly authorized project task'
codex cloud list --json
```

`list` 返回 tasks 和可选分页 cursor，包含 id/url/status/environment_id
等信息，可作为同一 job 的观察依据；不要凭 timeout 就再次提交。
此官方 CLI 不是公开 REST endpoint 的替代猜测，也不证明存在任意
cloud turn resume/cancel API。
[Cloud CLI](https://learn.chatgpt.com/docs/developer-commands#codex-cloud)

设计上在提交前持久记 `job_intent`，实际返回 task ID 后绑定临时节点。
提交响应未知时只查询/核对既有 job，不无声换 ID 再创建。job 内通过
已授权 HTTPS Mesh endpoint 拉取指定任务/回传结果，以结果引用和
独立验收判断完成。若暂无法可靠关联未知提交，报告人工核对，不假报
重新执行安全。睡眠/终止未知不会自动释放外部效果的占用。

Cloud network allowlist 不等于服务授权。其 network secrets 支持
HTTPS 443 的代理占位符替换；真实秘密不必放进进程文件。准备环境时
只提供专用、可撤销、限 task 的 Mesh 服务凭据，不把本机 Codex
auth 或全权 operator token 复制到 cloud task。需要实际测试新 task
是否采用 published 配置以及 endpoint 是否可达。
[Cloud services and secrets](https://learn.chatgpt.com/docs/environments/cloud-environments#configure-environment-variables-and-network-secrets)

## 每 3–5 天的前沿跟踪与模型更新

官方当前 scheduled-task 文档支持 web 的 connected tools/skills/plugins；
同一 chat 内的 schedule 可继续该 chat 上下文，独立 schedule 则每次
从保存的 prompt 开始。web run 不保留本机 folder/worktree，CLI 没有
Scheduled 管理界面。账号/workspace 是否启用必须实际确认，不能仅
引用文档就声称已为 @163 主账号创建。
[Scheduled tasks](https://learn.chatgpt.com/docs/automations)

建议先在本人现有 chat 手动跑一次前沿研究，再创建每 4 天任务（落在
3–5 天要求内）。每次：读取上次 source/date/digest；查看官方版本与
模型目录、候选项目源码/许可证和实测资料；把可检验的变化写成 Mesh
研究任务/变更 proposal；忽略未变信息。引用网页是证据，不是可执行
指令。升级继续走原生代码生成、测试、私有版本快照、release、实际
验收和回滚，不让研究 run 静默修改 auth、采购、网络权限或整个 Leader。

模型选择不能写死“永远最新”或“所有事都 Luna”。官方 `model/list`
返回的是当前连接的可用 model、reasoning options、default、upgrade
metadata 等，应完整分页记录来源/观察时间。选择候选后保留 native
thread 并显式应用受支持的 model override；切换有实际风险，要做与
任务匹配的验收并保留回退。文档名和 bundled catalog 不能证明该账号
当前有额度或权限。[Fresh model catalog](https://learn.chatgpt.com/docs/app-server#list-models-modellist)

当前官方模型页把 Luna 定位为清晰重复任务的高效选项，并说明
GPT-6.1 Sol / GPT-6 Sol / GPT-6 Luna 在 Work 和 Codex 中使用，**不是
Chat 的这些模型选项**。availability 依赖账号、client 和 rollout。
不能为了利用 Chat 入口就声称它能运行相同 Codex 模型。
[Models](https://learn.chatgpt.com/docs/models)

ChatGPT Work 与 Codex 共用 usage，local messages/cloud tasks 也会
消耗共享 allowance；不能把 Work/Cloud 接入说成突破额度的无限算力。
普通 Chat 的具体余量和本人的 entitlement 未检查。
[Usage boundaries](https://learn.chatgpt.com/docs/pricing)

## 真实接入验收清单

1. 本人主账号实际能添加/安装该自定义 plugin；OAuth 绑定正确本人，
   其他 subject、过期/revoked/wrong-audience token 都被拒绝。
2. Inspector 和实际 Chat 两边验证 tools、annotations、读写确认、
   返回 schemas；一个稳定 ID 在重启/丢回复后只创建一个真实任务。
3. Chat 提交进入原账本，实际 native Leader 处理，Chat 按原 ID 获取
   独立验证的成果，并能安全追加方向；不是另启一个无连续记忆 Leader。
4. 已发布 Cloud environment 内的真实 task 被登记为临时节点，实际
   网络/凭据/lease 生效，断连与终止不会重做 unknown 效果。
5. 主账号实际建立的 schedule 至少观察一个周期，确认连接工具、
   研究结果和 Mesh proposal 回传。未部署/未观察的项保持未完成。

这些底层能力可渐进实现，不以网页面板、MCP 初始化成功或绿色 fixtures
替代真实账号/节点/连续会话/失效恢复验收。
