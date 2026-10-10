# Firefox 普通 Chat 手动入口 v1

这是独立编写的 Firefox WebExtension + 本机 Native Messaging 小桥接，
不是 OpenAI 官方 Chat MCP、ChatGPT 插件或账号认证。已交付源码、
无签名开发 XPI 构建器、私有 host 模板和离线测试；Root 已在 Firefox 155
账户设置核对本人主账号，临时加载开发 XPI，并安装专用 ingress peer
与本机 host。实际浏览器单次提交、原 Leader completed、Root 独立审查
摘要发布和原 ID 批准成果读回全往返已通过，详见下方真实验收记录。
这只是手动桥接，不是 Chat 模型原生 tools；源码已公开发布，当前两端
正式加载的核心代码为 `e2397f1`，两端各 1267 项与公开 CI 通过。
它不安装 OpenClaw/opencode，不启动新模型，不改变原生 Leader 的连续
会话或任何节点的 Shell、文件、联网、MCP 能力。

## 操作与边界

本人在当前 `https://chatgpt.com` 页面打开扩展，输入要交给 Mesh 的任务，
或点击“读取本人选中的文本”，检查文字后保存稳定 ID，再勾选确认并
手动提交。选中文字只是一次固定的 `window.getSelection().toString()`
读取，不提交任务；页面/模型文字只是待审查数据，不会执行工具指令。
结果只用 `textContent` 展示，不插入聊天编辑框、不点击发送、不自动开 URL。

扩展只有 `activeTab`、`nativeMessaging`、`storage` 三项权限，没有
host 权限、content script、页面 postMessage 入口、外部扩展消息入口、
DOM 观察、cookies、登录信息、全部会话/历史读取或遥测。background 每次
操作都检查消息来自固定 ID 的自身 popup 且当前活动页的 origin 精确为
`https://chatgpt.com`。`activeTab` 是明确用户操作后获得的临时活动页权限，
不是整个账号读取授权。[MDN activeTab](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/manifest.json/permissions#activetab_permission)

ChatGPT 登录身份**不由 URL、extension ID 或本地 ingress token 证明**。
安装/真实验收必须由 Root 按 computer-use 规则在 Firefox 核对本人
主账号，保留原账号；不得为了可用性换账号。此桥接可以向 Mesh 派任务，
不能使 Chat 模型原生自动调用 Mesh、授予无限推理或赋予定时常驻能力。

## 稳定 ID、并行与恢复

`storage.local` 的单个有界状态值保存每个请求的完整正文、随机稳定
`mesh-firefox-<32hex>` ID、正文 SHA256、回执状态和当前选中 ID。在任何
submit 前先持久保存正文和 ID，再标为 unknown，然后才调用 native host。
此 SHA 只检测本地精确正文变化，不证明正文安全、语义唯一或执行质量。
本地状态不是加密保险箱；不要粘贴密码/token，其他入口成果的 DLP 也
不代替本人审查。

- 每次 submit/status/result 只对当前保存的原 ID 发一个请求；不自动重试、
  换 ID、查询轮询、批量派发或 replay unknown。丢回复后先查原状态，若需
  再提交仍需本人确认且使用同一 ID/正文，authority 保证幂等。
- “新独立任务”是本人显式的新意图：旧任务可仍在运行，所有原 ID 和正文
  保留，可用历史选择回到旧 ID 查询/重试。切换/选择本身不调用 native host。
- 在已保存的未完成请求中遇到同一精确正文 hash，会回到原 ID，不生成
  替代任务。已完成后本人明确开启独立周期任务可使用相同文字和新 ID。
  这不声称能判断不同措辞是否是同一语义意图。
- 首片最多保存 64 个请求，不自动删草稿、淘汰 unknown 或截断历史。满额
  时拒绝追加新 ID，旧记录仍可选择和回查；安全导出/清理 UI 尚未实现。
  卸载、清空扩展存储或改 extension ID 会丢本地回查索引，不应作为恢复办法。

权限限制属于此可选入口，不限制 Mesh 或原生 agent 的多任务能力。
任务正文、项目/希望的 agent 标签是开放的模型可读信息，标签不能指定
native session、角色、权限、任务 lease、执行能力或绕过 owner 的 Mesh 管控。

## 本机 host 与 authority 的固定契约

固定 extension ID：`personal-assistant-mesh@gih10012.local`。
固定 host 名：`personal_assistant_mesh`。Firefox native host manifest 使用
`allowed_extensions` 绑定 Gecko ID；Native Messaging 使用 UTF-8 JSON，
前缀为四字节本机字节序长度。此实现主动采用远小于浏览器上限的边界。
[MDN Native Messaging](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/Native_messaging)，
[MDN native manifest](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/Native_manifests)

Linux 部署的 owner 审查 launcher 固定源码和私有 client 配置路径。
`assistant_mesh.native_messaging.main(config_path, browser_args=...)` 要求
Firefox 的 manifest 绝对路径和固定 extension ID 两个参数；manifest 参数
不作为配置选择器。浏览器消息不能传任何 URL、header、bearer、配置路径、
shell、任意 RPC method、actor/subject/owner、全局 task ID 或原生上下文。
此调用方 ID 检查不是同 UID 主机上恶意进程的密码学隔离。

每个 `sendNativeMessage` 进程只处理第一个消息和一次 RPC，然后退出：

| 消息 action | 固定 POST 路由 | payload |
| --- | --- | --- |
| `submit` | `/v1/ingress/tasks` | `request_id,input`，可选项目/agent 标签 |
| `status` | `/v1/ingress/task/status` | 仅 `request_id` |
| `result` | `/v1/ingress/task/result` | 仅 `request_id` |

外层必须恰为 `{schema:1,action,payload}`。stdin 单帧至多 32 KiB，任务
正文至多 16 KiB UTF-8，stdout 单帧至多 64 KiB；错误只有固定类别和
unknown/rejected/not_attempted，不回显异常、路径、HTTP 私有正文或凭据。
非有限数、重复 JSON key、非法 UTF-8、截断/过长帧一律拒绝。
stdout 无日志；也不向 Firefox 可见 stderr 打印秘密。

专用 client 是 owner 部署的私有 JSON，必须是 owner-owned 0600 常规文件、
0700 直接父目录，无 symlink/hardlink/路径 `..`；祖先必须为 root/owner
控制且不可被其他人写入，root/owner 的 sticky 共享祖先（如 `/tmp`）除外。
这不是针对同 UID 恶意并发进程的 openat 路径隔离。字段只有
`control_url,token_file` 和可选 `unix_socket`。token 文件同样检查，使用
既有 `worker.Client` 的 TLS 或 loopback/私有 socket、不重定向、不使用
环境代理和 15 秒超时；HTTP 响应沿用 8 MiB 上限，再作封闭字段投影。
主机不会搜索 Codex 登录、读 ChatGPT cookie 或申请新 API key。

部署方必须在 authority 创建**唯一专用 ingress peer/token**，配置固定
owner/source/subject，scope 为 `tasks:submit,tasks:read`，且不得复用
operator/worker token。client JSON 不自报角色；实际权限仅由服务器认证的
peer 决定。既有 authority 只允许 ingress role 使用上述三路由，并按绑定
source/subject 隔离，operator/worker 不能借此路由提升权限。
Root 已配置独立 peer，并通过真实浏览器请求取得该入口的原任务回执；
这不替代其他 subject/撤销/越权的独立实测，离线 mock 也不证明实际
部署权限完整。后续安装仍须核对真实 token 与部署绑定，不能复制 fixture。

status 只回受限任务元信息，不返回 checkpoint、native history、原始
`task.result`、账号、全局 task 状态或其他 subject。result 只读既有
owner 明确审查后发布、绑定实际 completed task 的摘要及 HTTPS 公共引用；
不调用 publish，不能传 `reviewed` 自认审查。URL 不抓取，declared SHA 不
当内容验证，始终显示 `execution_verified:false`、
`artifact_content_verified:false`。参见 [入口契约](INGRESS-CONTRACT.md)、
[成果契约](INGRESS-RESULTS.md)。原生执行/effect ledger 不受桥接改写。

## 真实浏览器验收记录（2026-10-10）

Root 在 Firefox 155 的账户设置确认本人指定主账号；未换账号。开发
XPI 为临时加载，专用 native host/ingress peer 已安装。这不是 AMO
发行、永久安装、官方 Chat 插件或 Chat 模型原生 tools 的验收。

按授权进行人工确认后的原稳定 request：
`mesh-firefox-05d0bce8d4728cd553d831b9317529e3`。
实际 browser submit 只调用一次，收到 accepted、`task_created=true`，
绑定 task：
`ingress-c3d8bbe11285b26aab751c96b9dd8fe19229aea0bdb78900f6becf44c8799b76`。
随后按同一原 request 的 status 查询已验证；没有为了失败另造 ID。

历史观察中任务 `waiting_backend` epoch 5，原生阶段诊断为
`native_restore/native_session_destination_conflict`。独立只读核验
当时权威最新完整 history 为 9964417 字节；candidate 0 的目标为
9160820 字节，是其严格旧字节前缀，不是分叉；candidate 1 持有相同
最新 history。该轮 `native_start_attempted=false` 不是所有 epoch 零效果的证明，
不得清除效果标记、换 request/task/thread 或把重放当恢复。

尚未完成：其他 subject 的真实越权/撤销测试、Chat 模型自动工具回路。
Cloud 仍无 job/临时节点。
Root 的首轮维护因 Python 3.6 不支持 backup() 在 history publish 前
失败；finally 确认 worker 恢复 active，历史未改。随后在 epoch 15 静止
边界，用完整一致 read-transaction SQL dump 备份，保留源 9964417 字节
和旧目标 9160820 字节的完整原字节。核对严格前缀、已终结原生 turn、
未变源/目标/authority 后，在同 canonical path 原子刷新完整历史。
维护前后原 task/native authority 行一致，效果标记未改、DB 未 restore，
仅该 worker stop/start，其他服务保留；没有新 request/task/thread。

随后实际 operator/status 核验原 browser task completed epoch 16，
原 thread `01a116fc-8aae-7001-a0e2-07a1073c5bcb` 的 native turn 为
`01a1217d-f3e4-7282-878b-5c4e44a15173`。Root 独立 review 原结果后，
用固定 publication ID `PAM-006b-firefox-result-20261010-v1` 成功发布
审查摘要：`task_binding_verified=true,published=true`，未转发原 native
result。Firefox 实际按原 request 查到 completed，再点击读取批准成果，
收到 `ok=true,action=result,published=true`，原 publication/task/request
精确匹配。**手动 browser 任务/批准成果全往返已验证**。

成果的 `execution_verified=false`、`artifact_content_verified=false`、
`account_verified=false` 保留；界面主账号核对不等于此成果的账户认证。
这份原成果发布时目录/remote projection 为空、004b 仅 stage；它是当时
任务的快照，不随之后的安装更新。后续 Root 已实测 Codex MCP 三工具
提交/完成/批准成果往返，004b 已发布和激活安装绑定、配置容量及新 probe；
模型第一轮选择 waiting_evidence，当时未预留或执行。此后原 SELECT
同 task/thread epoch 2 已 completed，真实模型选择、单次 owner executor、
独立产物核对及结算/容量归还通过，见 [出口验收](EGRESS-CANARY.md)。详见
[MCP 入口](MCP-ENTRYPOINT.md) 和 [执行计划](PLAN.md)。不把手动往返外推为 Chat 模型原生 tools、Cloud 入网或
整体 goal 完成。

## 安装方式与剩余验收

现阶段只提供 Linux Firefox 开发入口，Firefox 155 的实际提交/状态
路径已观察；Windows 的 launcher/注册表、macOS 路径、Android/mobile
及 AMO 签名仍未实现/验证；已验收的是这一个手动 browser 往返，不是
所有权限/异常路径、平台或自动工具回路。
不要把“Firefox manifest 格式正确”表述为实际安装或账号接入成功。

1. 审查源码与测试，运行 `python browser/firefox-mesh/build.py <新XPI绝对路径>`。
   构建器仅打包五个 extension 文件及仓库 LICENSE，不含测试、模板、
   client、token 或任何 host 配置；拒绝覆盖已有输出，返回实际包 SHA256。
   产物是**无签名开发 XPI**，不是通过商店审核的正常发行安装包。
2. Root 在部署 authority 时配置专用 ingress identity 和唯一 token；私有
   client 可用本人已有受管 loopback/Unix socket 入口，或合法 HTTPS。
   配置示例只描述 schema，实际 token 不能放入 repo 或 XPI：

   ```json
   {"control_url":"http://127.0.0.1:18765",
    "token_file":"/ABSOLUTE/PRIVATE/firefox-ingress-token"}
   ```

3. 用 `launcher.py.example` 作为 owner 私有安装副本，审查并固定
   `SOURCE_ROOT`、`INGRESS_CLIENT`；launcher 应 owner-owned 0700、父目录
   0700。host manifest 的 `path` 必须是这个绝对可执行路径，不能仍是
   template。Linux 用户级 Firefox host manifest 通常位于
   `~/.mozilla/native-messaging-hosts/personal_assistant_mesh.json`；使用
   `native-host.json.example`，保留唯一 `allowed_extensions`，不增加通配。
4. 按获授权的 computer-use 规则保留工作的终端、处理最低亮度/恢复，
   仅在本人指定 Firefox 主账号开发临时加载 manifest/XPI，不能切账号。
   Root 已完成临时加载与 host 注册；普通发行版 Firefox 的永久安装
   可能需要签名，此次没有关闭签名/安全机制来获取永久安装。
5. 真实验收需分别证明：主动选择不发请求、本人手动提交到专用 subject、
   原 request ID 重查/重试不新建任务、不同独立任务并行、其他页面不可用、
   不能读其他 subject、没有 operator token、批准成果不泄原结果。先用
   唯一无害 canary，失败保持原 ID。真实 owner/账号/host 安装证据必须由
   Root 留存；离线 mock 不替代它。

## 离线测试与来源

运行 `python -m unittest discover -s tests -p test_native_messaging.py -v`。
28 项 Python 与 19 个 JS 离线 cases 已通过。它们包含在入口源码本机
完整 1240 项回归。入口源码已以 `7fb001a` 公开发布，CI 37960801403
实际 success；正式核心服务仍为 cf308b3，两端/CI 1148 项证据独立保留。
VPS Python 3.6 的两处测试参数兼容性修复后隔离 1240 项通过，未换系统 Python。
Python 测试覆盖 framing/固定路由/身份注入/secret 错误投影/私有文件/闭合
成果投影；有 Node 时再运行同包 `test_background.js` 的 mock 检查，覆盖
主动选文、source sender/origin、先持久后发送、丢回复/重开、并行任务、
原 ID 选择与精确正文 dedup、上限不淘汰。测试不启动浏览器、联网或模型。

已按 [接入研究](ENTRYPOINTS-IMPLEMENTATION.md) 评估第三方
MCP-SuperAssistant 的 MIT 许可证与宽域 adapter/analytics 权限。本小桥接
没有复制其代码/选择器/parser，不捆绑其遥测/依赖，也不假装它能提供官方
Chat 工具。新代码沿用本仓库 MIT LICENSE；Native Messaging 事实参考上述
Mozilla 官方文档。离线 package hash 仅绑定实际文件，不证明兼容性/授权。
