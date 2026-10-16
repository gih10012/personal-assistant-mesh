# ClawBot 入口的私有附件桥

叶任务：`confirmed_profile_binding`；分支 `work/entrance-media-20261016`，
base `2eceff0`。遵循 `owner-entrance-20261016-duplex-v1`，仅实现原消息
附件与时间/关联的桥。独立入口会话、双方历史、决定回复与 Leader 转交
由入口 core 集成；本叶没有修改原 Store、Worker、server 或通道游标。

## 缺口和边界

既有私有 `inbox.body` / 受认证 `communication_store.event().message`
保留 iLink `item_list` 原引用；公开 archive `items` 则刻意去掉了 CDN/AES。
因此不能从脱敏历史捏造可下载引用，更不应为补引用另开 `getupdates`。
`EntranceMedia.stage` 直接接收原受信消息，私存每项引用并返回不透明
`attachment_id`；`materialize` 只按已暂存 ID 下载。模型参数不能提供
CDN URL、AES key、脚本路径或新的授权。调用者仍负责原账号/事件认证；
本地摘要不是平台签名，也不是获得新权限的依据。

下载复用已安装 **wechat-personal** 的 `ilink_media.download_async` /
`transfer`，加密实现来自 `wechatbot-sdk==0.3.0`，不重写协议或密码算法。
原组件限定微信 HTTPS、不跟随重定向、限制明/密文大小，并核服务端有
提供的文件长度与 MD5。本桥保留完整明文字节、SHA-256、大小和原关联。
无需桌面微信、本人电脑或 account.json；没有 login/poll/send/cursor
操作，也不会自动安装 SDK、开 key、调用模型或上传附件给其他服务。

`available` **只表示下载并保存成功**，始终
`understanding_verified=false`。原生入口/Leader 应继续使用自己的
`view_image`、Shell 或合适解析器实际读取内容，保留原生安装/联网能力。
语音 SILK 下载不是转写，视频、未知 item 或过期引用也不得伪称已理解。
不支持的 item 私存原引用并返回 `not_supported`；下载失败返回有界的
缺 SDK/超时/来源拒绝/大小或摘要不符/HTTP 类错误，不泄漏原异常凭证。

## 接线合同

```python
from assistant_mesh.entrance_media import (
    EntranceMedia, SkillMediaDownloader, message_metadata, public_projection,
)

# 仅 owner 私有配置 / 安装代码提供这些参数，不能来自聊天或工具参数。
downloader = SkillMediaDownloader(
    media_helper_file='/PRIVATE/skill/ilink_media.py',
    helper_sha256='<owner verified file SHA-256>',
    ilink_sha256='<owner verified sibling ilink.py SHA-256>',
    python='/PRIVATE/fixed-sdk-python-launcher',
)
media = EntranceMedia('/PRIVATE/entrance-media', downloader=downloader)
staged = media.stage(
    original_message, source_ref=original_inbox_id,
    received_at=original_inbox_created_epoch_seconds,
    processed_at=None, request_id=original_request_id,
    task_id=original_entrance_task_id, reply_to=actual_reply_id_or_none,
    conversation_id=actual_conversation_id,
)
receipt = media.materialize(staged['attachments'][0]['attachment_id'])
# receipt['path'] 仅供获授权原生 agent 上下文；不是公网文件 URL。
operational_summary = public_projection(receipt)
```

无 downloader 的显式状态是 `entrance_media_not_configured`，不是静默走
本机旧账号或另开 receiver。`stage` 仅私有本地写，可在原入站事务内调用；
事务回滚最多留下无引用本地记录，绝不因此开始网络/任务执行。相同输入
字节与元数据产生相同 ID；所有原记录/receipt 为完整原子 first-write，
不覆盖旧数据。再次读取验证缓存完整字节，坏缓存拒绝而非悄悄重新下载。

`message_metadata` 的参数与 `stage` 相同（不需要 media 实例）。平台 ID
只接受整数/字符串并无损转字符串，不走 JS/float；原 `create_time_ms`
保存 raw 字符串、明确单位和 UTC，缺失或非精确整数标 unknown，不拿
received/processed 冒充。接收/处理/实际发送时间各自独立；来源、方向、
会话、reply-to、request/task 引用均保留。存储 UTC，显示注明
`Asia/Shanghai`；跨日结合真实日期显示。此函数不猜缺失 reply ID、
conversation ID 或实际发送时间，也不因下载时间更新原消息时间。

## 部署约束（由 Root 单线执行）

旧 core 可在 Python 3.6 import / stage，不需要 SDK。SDK 是 lazy 的短命
子进程，需要 Python >=3.9；既有 VPS Python3.12 Docker 层可用，不需要
切换整个原 Worker 解释器。把 **现有** `ilink.py` 与 `ilink_media.py`
复制到 owner 安装目录，保留相邻文件名；只装这两脚本和 SDK 依赖即可，
不用完整 CLI/ncut/账号。安装者应从读过的现有源核 hash，再填私有配置。

本叶实际核过的现有源 hash（不是让未来版本盲信这些旧 hash）：

- `ilink.py`: `e34968acfc8ecdedd542d101ad84197a1da45c66362a4efa75e17144c50a65ef`
- `ilink_media.py`: `31ab57cf8317523280d41553b0f66b72a637da17289aff01e46b5c0cd054994b`

`python` 可以是 owner 安装、不可组/其他人写的固定 launcher；它须把
`-I -c <download-only code> <helper directory> <two source hashes>` 和 stdin
原样交给已 pin 的现代 Python/container。容器内外 helper 路径须一致，
read-only mount 两脚本并使用正确 owner UID；stdin 只有本次私有媒体引用，
stdout 只有解密字节，stderr 丢弃。不得记录 stdin、复制账号或挂载整个
home；不通过模型拼接 docker/shell 命令。容器命令/镜像/网络由安装者
固定，脚本本身没有安装/登录或后台 receiver。这里的局部下载隔离不是
对原生 agent 的宿主 Shell/文件/网络限制。

状态 root 必须绝对路径，无 symlink，owner0700，直接父目录已存在；记录、
明文和 receipt 为0600，子目录0700。默认32MiB，可明确设到100MiB；这是
资源上限，不是微信支持所有格式/大小的承诺。名字仅作原始内容字段，
输出路径只使用 opaque ID 与安全后缀。`public_projection` 去掉绝对路径、
文件名、消息与会话/任务引用、所有原媒体凭证；完整 receipt 仅交私有上下文。

## 已验证 / 仍待实际验收

12 项合成测试覆盖原 ID/时间精度、私有暂存幂等、完整二进制/关联、坏缓存
拒绝、路径/权限、无配置、未知 item、子进程委托与源码 pin 漂移；没有真实
账号、CDN、模型或消息发送。另用当前真实 SDK 密码实现和两个真实已 pin
skill 文件，在**合成 HTTP transport**中验证完整明文相等、MD5 错误拒绝、
非法来源在 GET 前拒绝及禁 redirect：真实 CDN 请求为0。Python3.6语法
检查通过，不冒称已在实际旧宿主运行所有下载依赖。

尚未部署本叶、没有新入站图片/文件下载，也没有替入口 agent 理解内容。
Root 接线后应让本人微信发送一张图片/一个文件，核原 ID/时间、VPS 原附件
完整下载、原生 agent 实际阅读与自然回应；无需扩展无关全量测试。双方历史
与消息发送仍须由 core/原授权 CLI 保留原 request/status/unknown 语义。
