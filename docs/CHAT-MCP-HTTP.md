# 普通 Chat 的受限 Streamable HTTP 入口

此片是现代 MCP resource-server 侧车，不是另一个 Leader。只暴露既有
`mesh_submit_task`、`mesh_task_status`、`mesh_approved_result` 三个工具；
原 [stdio 入口](../assistant_mesh/mcp_entrypoint.py)、核心 Store/Worker、
原生 Shell/文件/网络/MCP 和旧来源身份均未更改。

源码见 [mcp_http.py](../assistant_mesh/mcp_http.py)、
[认证适配](../assistant_mesh/mcp_http_auth.py) 和
[测试](../tests/test_mcp_http.py)。目前只验证隔离本机 TCP/ASGI/SDK 协议。
没有公开 HTTPS 部署、真实 IdP OAuth 登录/同意、主账号 Chat 安装或模型
工具调用，不能称“Chat 已入网”或无限额度。

## 当前官方接口与选型

OpenAI 要求带个人数据/写操作的 MCP 服务验证用户身份；服务是 OAuth
resource server，现有 IdP 是 authorization server，ChatGPT 是客户端。
推荐已有成熟 IdP，不手写登录页、code/refresh token 或自制授权服务器。
MCP resource metadata、issuer metadata、PKCE S256、resource→audience、
issuer/expiry/scopes 都需符合实际客户端合同。公开 Chat 不能拿一个
customer API key、operator bearer 或本机 Codex auth 代替 OAuth。
[OpenAI Authentication](https://developers.openai.com/plugins/build/auth)。

2026-10-12 已实际安装并检查官方 Python MCP SDK `2.3.0`（2026-10-02
发布，Python>=3.10）。此片用 `MCPServer` + `TokenVerifier` +
`AuthSettings(validate_token_resource=True)` 的真实 Streamable HTTP ASGI
app，不自行实现协议。SDK 默认 resource validation 尚非 True，因此
这里显式打开。SDK 发布 RFC9728 metadata 与 401 challenge。
[官方 SDK release](https://pypi.org/project/mcp/2.3.0/)，
[固定版本 auth source](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/src/mcp/server/auth/settings.py)，
[SDK authorization](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/docs/run/authorization.md)。

最新 `2026-07-28` 协议使用 `server/discover`，不再用旧 `initialize`
握手，并在每条请求带标准 `_meta` envelope。实际 SDK Client 的 auto
模式负责新发现和旧协议回退；不要给最新协议头手拼旧握手。侧车已
分别实测新协议 Client 和 `2025-11-25` 原握手兼容。
[官方 protocol versions](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/docs/protocol-versions.md)。

## 鉴权和权限边界

`validate_policy(document)` 是部署配置纯校验，不等于 OAuth 已接通。
配置固定 canonical HTTPS `/mcp` resource URL、现有 issuer、其已发布
metadata、owner binding、实际 IdP `sub`→稳定 Mesh subject 的映射、
实际允许的 OAuth client IDs，以及 owner 最大批准 scopes。
输入不能选择 source/role/native session/operator；source 固定为
`chatgpt-chat`。邮箱域名、client ID 或“浏览器已经登录”都不是 owner
身份，必须由现有 IdP 的受验证 subject 映射。空配置/缺 verifier/缺
backend/未装固定 SDK 返回 `mcp_http_*_not_configured`，没有匿名兜底。

`OwnerTokenVerifier(delegate, policy)` 实现 SDK TokenVerifier：delegate
必须验证签名或 IdP introspection，不能仅 decode JWT。wrapper 再核
issuer 精确字符串、资源、到期、批准 subject/client 和 scopes 交集。
SDK每条 HTTP 请求验证；工具调用再次核同 principal/到期，并从 SDK
context 获取身份，不接受模型 JSON 自报。只有经过 owner 批准的 scopes
才能使用；拿到 `operator` 等额外 scope 不能扩大 Mesh 权限。

可插入现有 IdP SDK/introspection TokenVerifier。可选 `IdPJWTVerifier`
使用 PyJWT + owner 固定的 JWKS URL，只允许配置的非对称算法，核签名、
iss/aud/exp/sub/client_id 或 azp。token 自带的 URL/role/owner 字段不
决定路由。JWT 不保证即时 IdP 撤销：应使用短 TTL/撤销映射，或接现有
introspection verifier；不能声称无条件即时撤销或完整 OAuth 生命周期。

公开三个工具都有 top-level `securitySchemes` 和 `_meta` 兼容镜像。
owner 已允许写入但当前 access token 仅 read 时，写工具返回
`_meta["mcp/www_authenticate"]`，包含 resource metadata、scope 和标准
error/description，以提示 OAuth 重新同意。它不会替 owner 批准新权限。
[官方 tool-level auth](https://developers.openai.com/plugins/build/auth#triggering-authentication-ui)。

## 复用原 authority，不打开 operator RPC

`LocalIngressBackend(existing_ingress, existing_results)` 复用原对象和事务，
构造固定 role=ingress/source/subject/scopes。不创建 Store、不触发旧
constructor recovery，不返回原 native result 或历史。

独立环境也可用 `RemoteIngressBackend({mesh_subject: existing_client}, owner)`。
各 Client 必须由 owner 安装为独立 ingress peer，在实际 authority 配置
中绑定 `chatgpt-chat` 和同 subject。只请求以下三个固定路径，access
token 不被转发到后端；不能用全 operator token 或 path 名当角色证明。

| Tool | 固定 authority route | 可读取内容 |
| --- | --- | --- |
| `mesh_submit_task` | `/v1/ingress/tasks` | 原 request/task 接受回执 |
| `mesh_task_status` | `/v1/ingress/task/status` | 自身来源的有界状态元数据 |
| `mesh_approved_result` | `/v1/ingress/task/result` | 独立 owner 发布的 summary/public references |

原 request ID 和内容依靠 Ingress 持久一次绑定；HTTP/MCP session/模型
变化不会改变它。掉网或不可信回执只记 unknown，不自动 retry、更换 ID
或重发；可按原 ID 查询。项目/agent label 是请求，不是 session/权限。
这里没有公开 continuation/control/shell/discover-provider 工具；新增原
会话续接仍通过原 authority 的显式授权合同，不能从 read/status 提权。

所有响应复用原 native_messaging 的闭合 projection：不带 raw result、
private paths、token、其他主体任务或未经批准的 artifact 内容；完成
状态/摘要不升级 `execution_verified` 或 `artifact_content_verified`。
只读 Chat 并非整个 Mesh 的上限，Leader 及原生工具继续自主工作。

## 最小启动交接（部署例，不是自动执行）

1. 在仓库外 Python>=3.10 私有环境安装
   [optional requirements](../deploy/mcp-http-requirements.txt.example)。
   旧 Python3.6 核心不加依赖、不换 interpreter。
2. 在实际 existing IdP/domain/owner 同意已成立后，填写
   [private policy example](../deploy/mcp-http-owner.json.example)：metadata
   必须来自现有 IdP，PKCE S256/注册方式/准确 Chat callback/resource aud
   在该 IdP 上真正可用；本例 `.invalid` 不提供登录。
3. 复用实际 owner-configured ingress/result；或独立安装固定源的窄
   ingress peer 和私有 Client refs。不能复制 operator bearer 到公开侧车。
4. 把 [factory example](../deploy/mcp-http-owner.py.example) 按环境适配到
   私有部署模块，指定 `MESH_MCP_HTTP_CONFIG`；已核权限后可用如下形式
   启动到 loopback，再由 Root 接既有 HTTPS/认证代理：

```sh
/PRIVATE/venv/bin/python -m uvicorn private_mesh_factory:create_app \
  --factory --host 127.0.0.1 --port 3289 --no-access-log
```

这是示例命令，没有在生产运行。`create_server(config, token_verifier=...,
backend=...)` 返回 `(server, app)`；构造不打开监听。直接让 uvicorn 运行
app 保留 SDK lifespan；若挂入其他 ASGI app，host lifespan 必须进入
`server.session_manager.run()`。不要只 mount 后宣称启动成功。
[官方 ASGI lifespan](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/docs/run/asgi.md)。

resource URL 必须等于实际 Chat 连接地址/IdP audience。Host allowlist
只允许此地址，Origin 默认不允许额外浏览器 origin；需要浏览器入口时
在 owner config 配准确 origin 和 CORS，不关 DNS-rebinding 检查。TLS
代理需保留正确 Host、禁止未经信任的 forwarded header，并限制请求
体/日志/资源。默认 body 64 KiB，业务文本沿用既有 16 KiB 上限。

OpenAI-managed mTLS 可以认证 ChatGPT 客户端，但不能代替 owner OAuth；
IP allowlist 也不是身份。本片未改域名/DNS/防火墙/云账单、未注册 OAuth
client 或发布插件，私有 config/client/token 均不得进公开仓库。

## 验证边界

16 项聚焦测试全部在私有 SDK2.3.0 环境通过，包括真实 uvicorn TCP
listener/HTTP、实际 SDK Client 最新 discover、旧协议 initialize、三
工具 submit/status/批准结果、稳定 ID、未 auth/错误 issuer/audience/
scope、跨 subject、Host/Origin/body 拒绝及内存合成 RSA JWT 签名验收。
合成 IdP verifier 与内存密钥不是现有账号登录；不是 Chat 主账号模型。
首次用新协议头手写旧 initialize 的400失败保留，修正为官方真实 Client
和单独旧协议兼容验收，没有放松鉴权。

未装 optional SDK 的 legacy/core 测试只跑纯合同，并明确 skip 六项 SDK
试验；这种 skip 不叫 HTTP 验收。正式 Chat 入网还缺：真实稳定 HTTPS、
existing IdP OAuth+PKCE/resource/subject/client 同意、主账号实际连接、
模型真实提交原 request/读取批准成果和同项目续接。Root 完成这些实证
后再记录入网；没有就保持对应 not_configured，不用源码/测试数顶替。
