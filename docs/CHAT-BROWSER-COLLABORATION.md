# 普通 Chat 的按需协作通道

本片是**源码与离线合同验收**，未安装扩展/native host、未操作本人账号、
未创建或发送真实 Chat turn，不是 Chat 原生 HTTPS/OAuth MCP 已接通。
目标是连续 Codex/Pi Leader 按需把一个明确子任务交给本人普通 Chat，
无需等周期检查；Chat 回答经 DOM 观察持久回到 Mesh。原 Firefox 人工
入口、stdio MCP 和 core server/Store 完全保留。Live/面板另属后续项目。

## 已实现的最小边界

- `assistant_mesh.chat_bridge_queue` 提供 owner 私有的持久 enqueue/status/
  result/wake，以及独立长 Native Messaging host。SQLite `FULL` 事务先记
  原 request/task/内容 SHA，再允许一次 DOM send grant。
- **普通 Chat，不是 Work/dots/MCP Events，不是 OpenAI API。** 不读取
  cookies/token/auth，不猜私有推理 API、不自动登录、不偷偷换模型、
  不创建新会话或付费兜底。现有订阅的额度与平台限制仍适用。
- 独立扩展 ID `personal-assistant-mesh-chat@gih10012.local`，独立 host
  `personal_assistant_mesh_chat`；不覆盖人工入口的扩展或 host。
- Firefox 后台用 `connectNative` 保持事件端口。enqueue 成功后通过
  owner 0700 目录内 0600 Unix wake socket 通知，**不是任务定时轮询**。
  host 不在线时任务保留；下次连接自动读取原请求。重连退避仅恢复
  transport，不发送新的 Chat turn。
- 每项目固定本人已存在的 `/c/...` 会话；同一会话只绑定一个项目，
  同项目按未闭合原请求串行，不以新 ID 绕过未知 turn。其它项目仍
  可独立推进。account/conversation 变更拒绝认领旧队列。

模块是 trusted owner local 能力，不自带 Mesh lease/enrollment 授权。
调用者必须在现有原子任务合同下创建明确 child task，并保存 request
ID 后调用；不能把模型自报的 `task_id` 当执行权限。远端 Leader 可
按既有本人 A2A/SSH 将工作交给浏览器节点；本片不扩 operator/worker
权限，不阻断原生 Shell、文件、网络或其它 MCP。DOM 结果的来源是
`visible_dom_text`，不是隐藏推理、完整账号历史或独立执行认证。

## 一次提交与恢复

`enqueue` 私有保存精确 input（不 trim），生成送入 Chat 的原 request
与内容 SHA 前缀。相同 task/request/内容重查返回同记录；换内容、换
request 修补同 child 或同项目改会话冲突。扩展也私有持久保存原绑定。

提交前先确认：当前 URL 精确等于固定原会话、正常账号菜单中**明确
当前选中项**的本人主账号邮箱 SHA 匹配、空 composer、无生成中回复。
仅在 owner 已审核的 `dom.account_menu` 合同成立时正常点击只读账号
菜单/子菜单，读取选中项内 email 后关闭自己打开的菜单。任意聊天
正文、任意页面 email 文本、昵称或历史“已登录”标记都不是账号证据。
没有已验配置保持 blocked；`account_selector/account_attribute` 仅在
受核当前 selected 项内取 email，旧无 `account_menu` 配置不再被宽松
接受。不是读取 cookies、隐藏 store/API 或自动切换账号。

扩展 **先持久本地 send intent** → native host CAS 持久 `unknown` →
返回唯一 `send_granted=true` → 再填 composer 并只执行一次 send button
DOM click。绝不 Enter fallback。填入后再核账号/会话/生成中状态/
composer 精确内容及可用 send button；条件变更就是 unknown，且不
删除或覆盖用户随后写入的内容。

任意崩溃/断口/丢 grant ACK 后均只观察**原会话原消息**；不再次请求
grant、不重写输入、不换会话/账号/request。先开始持久 intent 后尚未
实际点击也可能保守停在 unknown，这是缺乏平台幂等接口的真实代价，
不是 exactly-once 推理承诺。原 unknown 可经 read-only status/result
及同 ID `wake` 对账，不能用“页面暂没看到”证明未发送并自动重投。

## 标签与成果边界

只创建 `active:false` 的专用**原会话**标签，附 `sessions` 私有 ownership
nonce；恢复同时匹配 tab ID、URL、owner/project/nonce。绝不按相同 URL
认领用户普通标签，绝不 activate/update 用户窗口、切换桌面焦点或
关闭其它标签。专用 tab 被用户导航、关闭或 ID 复用时不再控制它；
恢复可新开同原会话后台 tab，但 send intent 仍是 observation-only。

只允许固定本地 `chat_bridge_dom.js` 注入受管 tab，固定内容脚本动作；
不接通用 eval/Shell/page-private API。来自其它 tab、iframe、page 或
扩展的结果消息不能改变原请求。

完成须看到**唯一精确原用户消息**及对应唯一 assistant 消息，完整
`innerText`、两个消息 ID、经本人审核的回复终态 affordance、无 stop
或 streaming/busy，且第二次快照精确相同。中间新用户 turn、重复原
消息、缺失 ID、只有文本稳定或未知 DOM 都不判完成。MutationObserver
随 DOM 变化观察，不移动滚动条/焦点。120 秒只报告 partial/unknown，
不会把超时的前半答复叫成功；partial 继续观察后续终态。

不 trim 原文，不截取 Markdown 子块，不只保存 SHA/摘要。私有记录存
完整捕获文字、UTF-8 bytes/SHA、conversation/account 绑定及原消息
ID。超过 512 KiB 明确 partial/`visible_text_exceeds_capture_limit`，不
裁剪成成功；隐藏/虚拟化 DOM、未展开附件、图片、引用和隐藏推理不
冒称全部捕获。已 completed 结果不可变，关闭浏览器/撤销启用后仍
可按原 ID 完整读回；`execution_verified/account_verified=false`
始终保留。这个结果不是 Chat 本身原生 MCP 调用的验收。

## 最小安装交接（仍未执行）

1. Root 只读核对本人 Firefox **@163 主账号**及选定项目原会话 URL，
   审核当前 DOM account/terminal/composer/message selectors；别用
   聊天中的 email 文本或模型自报账号替代本人账户页核对。
2. 在仓库外本人 0700 目录复制 `chat_bridge_config.json.example`，填写
   实际原会话/主账号/owner namespace/selectors；config/request JSON
   0600、单链接普通文件，拒绝不安全 ancestor/symlink。示例默认
   `enabled=false`；本人确认后才启用，不能直接运行占位配置。
3. 固定本片冻结 runtime；复制 `chat_bridge_host.py.example` 到私有
   executable launcher，仅替换 runtime/config 常量，保留 Firefox
   supplied manifest-path 与 exact extension-ID 检查。示例 launcher
   可以使用本机 python3，但现代 Firefox 安装/签名由 Root 核验。
4. 用 `chat_bridge_package.py NEW_PRIVATE_XPI` 构建独立未签名包：仅
   generated manifest、两个 JS 与 LICENSE。包不带配置/聊天/密钥。
   Firefox 常规 release 的签名/加载要求单独核验；暂加载不等常驻
   安装成功。模板 host JSON 注册为本人 Firefox 的独立 native host，
   别覆盖已有 `personal_assistant_mesh`。
5. 本机 owner CLI 示例（所有路径/ID 均需替换，非已执行回执）：

```sh
python3 -m assistant_mesh.chat_bridge_queue --config /PRIVATE/chat.json enqueue --request-file /PRIVATE/original-child.json
python3 -m assistant_mesh.chat_bridge_queue --config /PRIVATE/chat.json status --request-id ORIGINAL_REQUEST_ID
python3 -m assistant_mesh.chat_bridge_queue --config /PRIVATE/chat.json result --request-id ORIGINAL_REQUEST_ID
python3 -m assistant_mesh.chat_bridge_queue --config /PRIVATE/chat.json wake
```

`original-child.json` 只有 `request_id/task_id/project_id/input`。工具参数
不能选择账号、conversation、model、URL、auth 或 native config。
主机 template 私有常量决定部署；Firefox argv 校验不是同 UID 文件
隔离。当前 native host 用 POSIX flock/Unix peercred/0600/0700，是
Linux 适配例，不虚称 Windows/手机已可安装；wire/任务合同可由节点
agent 适配。需要提供当前主账号 DOM 或扩展加载权时问本人，不用
付费 API/新 key/新账号/Work route 把验收换掉。

### 当前 Firefox 的正常安装路线

2026-10-12 只读元数据核验：本机 Firefox 155.0.1 为 release channel；
启动 wrapper 只 exec 本机 binary。检查到的 user prefs 没有签名关闭、
chrome/remote debugger/experiments override；正常配置位置未见 autoconfig。
企业 policy 只有 Extensions 项，未观察到本片可用的特权安装渠道。
这些是范围内元数据，不是对所有机器浏览器状态的穷尽证明。

旧人工入口的本人 native manifest/launcher 仍在，launcher 仍固定旧
冻结 runtime，`allowed_extensions` 只包含旧 ID。本片必须新建独立
`personal_assistant_mesh_chat.json`，不扩旧 host 的权限。被检查的
default-release 持久 addon 列表未出现这两个 ID；临时 addon 不由该
文件可靠证明，应由 Root 在当前 Firefox `about:debugging` 核验。
不据历史临时安装或文件存在声称现在已加载、常驻或实际连通。

开发验收可在 **`about:debugging` → This Firefox → Load Temporary
Add-on** 选择本片打包的独立 XPI（或将包展开后选择 manifest），只
加载独立 ID、不重载/修改旧人工扩展。该正常开发路线会在移除或重启
Firefox 后失效，不能称常驻安装；重载也不是再次发送任务的授权。
[Mozilla temporary installation](https://extensionworkshop.com/documentation/develop/temporary-installation-in-firefox/)

release channel 的长期正常安装需 Mozilla 签名。默认后续路线是
AMO **self-distribution/unlisted**：只上传不含配置/账号/聊天的源码包，
取回 signed XPI，再在 Add-ons Manager 的 Install Add-on From File
安装。此过程需本人 AMO 登录/提交授权，尚未进行；不因仓库公开就
推定可代签名或上传。`ExtensionSettings` 可管理正常安装，不是绕过
release 签名的机制。不为本片修改签名开关、安装特权补丁、切换浏览器
发行渠道或自动迁移本人 profile。
[Mozilla signing](https://extensionworkshop.com/documentation/publish/signing-and-distribution-overview/)，
[Mozilla enterprise distribution](https://extensionworkshop.com/documentation/enterprise/enterprise-distribution/)

Linux 的独立用户 host manifest 按正常 Firefox 位置注册：
`~/.mozilla/native-messaging-hosts/personal_assistant_mesh_chat.json`，
不是任意 XDG profile 子目录。`path` 为私有 executable launcher 的
绝对路径；Firefox 给 launcher 的两项 argv 是 manifest 绝对路径和
扩展 ID，模板已核对此合同。`connectNative` 长 port 由扩展后台维持；
browser/extension 退出会终止 host，不把进程存活当后台 Chat 推理可用。
[MDN native manifests](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/Native_manifests)，
[MDN native messaging](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/Native_messaging)

### 当前真实 UI 的 selector 核验

Root 可以在本人授权的正常 Firefox UI/页面 DevTools 核对公开 DOM；
只观察选定项目/账号控件，不读 cookies、页面内部 store 或私有 API。
只记录 selector、消息属性是否存在及脱敏核验结论，不能把完整邮箱/
聊天或页面 dump 放进公开仓库。具体 gate：

- `account_menu` 必须由实际 UI 审核，提供 1–3 步只读 menu opener、
  menu selectors/roles、明确 current 项及 selected 属性。每个 opener
  必须是唯一可见 button/menuitem，具正确 `aria-haspopup`、初始
  `aria-expanded=false`；正常点击后实际 `aria-controls` 必须精确指向
  唯一可见 menu/listbox/dialog 的 ID，后续 opener 仅在该已核菜单内找。
  opener/menu/current/email 在聊天消息或 composer 内一律拒绝。
- current 项必须唯一可见，具有明确 positive selected 属性，例如
  `menuitemradio + aria-checked=true`、`option + aria-selected=true`，
  或本人真实核验的 `data-selected=true/data-state=checked`；不能把
  “某行含本人 email”当 selected。email 只在该项内取。只关闭自己
  从 closed→open 打开的菜单，确认所有打开菜单都关闭；用户已经
  打开的菜单、不对应控制关系或无法恢复均 blocked，不切换账号。
  `account_menu=null` 是模板默认值，没有猜测 profile 按钮/选中图标。
- Root 当前只从正常 UI 观察到本人 @163 行右端 checkmark，其它账号
  无 checkmark；尚未核对该页面的 DOM selector、selected 属性或
  controls 关系。因此**还不是可安装的已验配置**。若当前 UI 只给
  视觉 checkmark、没有本片支持的正向语义，报告具体安装 gate后再
  按正常 DOM 适配，不用假的 radio 属性或此前账户页核验绕过检查。
- composer/send/stop selector 分别要命中当前编辑器/真实发送控件/
  生成中停止控件。不能选任意 role=button、页面文本“发送”，也不能
  将按钮暂无属性理解为平台已接受了原 turn。
- message selector/role/ID 属性要绑定**唯一原用户消息**及其对应回复；
  真实 UI 若 `innerText` 渲染改变了用户原文，不能靠 trim/模糊匹配认领。
  本片宁可 unknown，后续改装须仍保留精确原 intent 与可审计对应关系。
- completion selector 相对该 assistant 所在 `conversation-turn` 查询，
  必须在回复结束才出现可见终态 affordance，并同时确认 stop/busy/
  streaming 消失；“复制”按钮若生成期间也出现，不能单独作为终态。
  根节点、ID 或多块回复结构变化时要重新适配，不能把固定旧选择器
  和 200ms 文本稳定外推成真实完整性证明。

目前未访问真实 DOM，所以没有为当前主账号提供已验 selector 值。
页面现有账号/模型/额度和平台可用性均仍以 Root 的真实核验为准。

## 验收与当前证据

当前 28 项 Python 合同测试通过，另 24 项 controlled DOM/port cases
通过；原 Native Messaging、stdio MCP、CloudJobs 与本片合计 127 项
近邻回归通过。`git diff --check` 通过；这些都不是已安装或真实账号证据。

离线命令：`python3 -m unittest tests.test_chat_bridge_queue`；其 Node
子测试 `node browser/firefox-mesh/chat_bridge_test.js` 控制 DOM/native port。
覆盖 concurrent once-grant、原 ID reopen、原文与私有存储、完整终态
必要证据、partial 不成功、丢 ACK、错账号/会话/普通 tab 不干扰、包
allowlist，以及真实**本机私有 Unix socket/pipe** wake→offer→grant→
report→EOF cleanup。IPC 是真实 fixture，但 DOM、邮箱、会话和结果
均合成；未执行任何模型请求，不能拿绿色测试说已普通 Chat 入网。

Root 的一次真实验收：保持用户当前普通 tab/桌面不动，明确新 child
task 经事件即时推到选定原 Chat 会话；核仅一次 DOM send，完整答复
及原 request/task/message/bytes/SHA 一致。再独立 child 同会话续接，
测试一次 grant 后断口/重连只观察原 ID，wrong account/conversation
拒绝及浏览器退出后成果仍读回。登记“安装、账号、一次事件唤起、
同会话续接、终态全文、断连不重发”各自真实证据；未发生保持 pending。
本片不修改原 GOAL/PLAN/TASKS，由 Root 统一记录与公开发布。
