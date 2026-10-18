# 原 authority 上的完整 Claw 入口 CLI

叶任务 `confirmed_profile_binding`；`work/claw-authority-cli-20261018`，
base `ed613e9`（已审入口 core 的285021c）。这是源码/安装候选，不是已
部署或本人验收。旧延期 Chat case 在另一分支，本叶不处理其 guard。

## 一条真实路径，而不是第二套机器人

```text
VPS 原生入口 Shell
  → python -m assistant_mesh.claw_cli
  → 原 worker config / token_file / 当前入口 task+epoch
  → 既有 POST /v1/agent/action，新增 entrance_claw
  → owner 可选 ClawStore → 同一原 authority / outbox / 唯一 sender
```

服务端始终先 `_entrance_task(..., require_entrance=True)` 核原入口 scope、
实际 node 和当前 lease，不给入口 operator bearer 或整个管理 API。
原 server/Store/Worker/Codex/sessions 不改；owner 启用后只是把新的薄
Store subclass 包在已审 `entrance_store_class` 外。没有新 poller、iLink
cursor 消费、直接 sendmessage、后台服务、账号副本或登录动作。
Leader 原直接 Claw 权限/旧工具保持，这不是宿主 Shell/文件/网络限制。

| 命令 | 实际来源与行为 |
| --- | --- |
| `status` | 原 channel_error、outbox统计、上下文与媒体配置；配置不等于在线验收 |
| `history --source events\|inbox\|outbox` | 已审入口的原双方归档/分页；不推进 iLink 或 skill updates 游标 |
| `context` | 原 owner_context 的来源ID/原毫秒时间、接收/最近poll时间；不输出 token，validity仍unknown |
| `send-status --request-id ID` | 原 outbox 与本叶 upload reservation 状态；unknown 不自动重发 |
| `send-text --request-id ID --text-file FILE` | 原 `_enqueue(...,explicit=True)`；不是内部notice，也不是直接 iLink |
| `send-media --request-id ID --kind file\|image\|video --file FILE` | 原 ledger持久reserve→已有SDK上传→原explicit outbox/media_items；视频仅源码支持未验收 |
| `download --attachment-id ID` | 已授权入口附件membership→已审 `entrance_media` 私有原文件；下载不等于理解 |
| `recover-context --request-id ORIGINAL_SEND` | 原rejected发送/原renewals/上下文及既有refresher进展，不新claim或重发刷新 |

各命令仍保留原请求、消息、时间、回复和任务关联。常规对话优先用入口
`history → reply` 的最新消息检查；直接Claw发送是本人授权能力，不替模型
判断当前日期/话题，不能用它发过期模板或给新消息播昨天旧答。发送受理与
手机送达分别记录，所有回执仍 `delivery_verified=false`。

## 与 wechat-personal 的复用和实际缺口

已读主skill及ClawBot remote/history/send-text/send-media/media/
recover-context契约和现有CLI。普通旧 `bot updates` 在remote模式读归档但
推进它自己的本机read cursor；这里用原入口history分页，不调用它。
旧 `native messages` 只覆盖桌面已同步史，不当成VPS全部云端历史。
旧remote media需要本地account.json，新的独立communication broker还
明确fail-before-upload；不把原本机发送/媒体的既往验收套在新路线。

本叶媒体调用原 `ilink_media.upload`，AES/CDN/上传URL/媒体项仍由已安装
wechat-personal + `wechatbot-sdk==0.3.0` 实现。没有重写协议。authority
只读原 `config.ilink_account`，核仍是同bot/owner，取其三个必要字段
`baseurl,bot_token,ilink_user_id` 经私有stdin交给现有固定SDK launcher；
不复制整个account/history/context，也不把账号mount进容器。stdout返回
的加密媒体引用只存原私有ledger，不交模型或公开接口结果。Root 已选的
systemd-user/容器launcher可以原样共用；源码自身不sudo/docker、不改
NoNewPrivileges、dockergroup、服务权限、网络或账号。

context失效的正常路径仍是：原sender明确拒绝→既有authority原版本
renewal→已经授权的个人微信refresher发原标记→唯一receiver取得精确新
入站→原outbox最多一次受控重试。入口不能自己 `claim_renewal` 偷走
refresher reservation，更不能另开getupdates。`recover-context`展示
该实际原流程，不伪称已造出新用户上下文；无桌面/其他原生刷新provider时
标配置/实际缺项，也可等本人新入站。-14认证问题不自动重新扫码，unknown/
submitting不能因换模型/节点/ID触发恢复。这条物理依赖并未被本叶消除。

## 原 ID 和上传窗口

上传前在原ledger `claw_uploads` 预留原ID与kind/name/size/SHA，在
`claw_calls` 固定原call与参数。该reservation的reserved/unknown结果
只查原状态，不能自动第二次上传。完整上传成功后先私存原item，再核
当前lease、原ID和输入，才将其写入原outbox。同事务为新outbox行保存
media_items。上传期间没有SQLite writer锁。

上传已完成但lease失效仍保留原uploaded receipt，**不发送**；之后仅在
有效授权下完成同一原操作入队，不能重传已知上传。历史old media没有
新upload receipt时，仅读原outbox，明确original_content_revalidated=false，
不为比对而重传；文字/媒体跨格式复用同ID拒绝。原outbox已有unknown/
submitting、client_id、上下文和retry次数不改。失去RPC ACK用原ID状态
读取，不新建业务操作。用户自愿的新业务仍可新ID，不是宿主命令白名单。
既有控制Client的超时保持不变；较慢上传可能在RPC超时后仍由authority完成。
此时CLI只报告unconfirmed，按原request ID读send-status；不能当成未发生、
再次上传或换ID发。上传本身不会持有SQLite writer等模型处理。

## Root 最小接线（本叶未执行）

原receiver私有配置只新增：

```json
{"claw_cli":{"enabled":true,"max_bytes":33554432}}
```

缺省/false不启用。复用既有 `entrance_media` 的helper两SHA和固定
`python` launcher、原 `ilink_account` 路径。SDK运行需要Python>=3.9；
旧host3.6可import本叶，上传/下载都在同一短命现代子进程中。Root现有
镜像/readonly helper足够，不需要新账号mount/个人微信客户端/新key。
launcher必须完整转发Python `-I -c`参数及stdin/stdout，不记录stdin；
upload stdin为 `{state,data:base64,kind,filename}`，stdout是≤64KiB私有
item JSON，stderr不入模型，55s调用界限。默认文件32MiB；authority可明确
配到100MiB，CLI默认32MiB。此限制是局部资源界限，不是微信平台承诺。

`deploy/build-entrance-candidate.py` 现在追加两个Claw模块，原40模块仍按
已审builder核验。Root应把新source候选安装到唯一authority，不向正在
运行的数据目录随意混拷；原账号、数据库、IPC/carrier归还和备份规则不变。
不因安装CLI重启原Worker或恢复旧任务。旧CLI/remote配置也不被重写。

入口角色reference提供实际worker config路径、当前task/epoch及命令列表。
该config只有原worker peer权限，不能换成operator配置：

```sh
python3 -m assistant_mesh.claw_cli --config /PRIVATE/entrance-worker.json \
  --task-id ORIGINAL_TASK --epoch ORIGINAL_EPOCH --call-id ORIGINAL_CALL status
```

其他命令沿用同前缀，发送的request ID也是同一业务的原ID。文件路径是
**执行authority所在节点**的本地适配，不是跨设备文件传输协议；CLI先核
owned regular file完整SHA/名字/大小，authority再次核真实文件字节。别的
节点须先通过Mesh已有artifact能力送达或自己用当地能力适配，不能把
手机/Windows路径假装为VPS文件。原生Shell仍可采用其他获授权工具。

## 已有验证与后续实际验证

10项新检查使用真实SQLite/精确原3198 Store +已审入口，原生CLI parser
也实际进入该authority适配：文字和媒体确实进入原outbox，旧unknown/
client_id/格式冲突保留，上传一次、错误不重传，上传后lease失效保存结果
但不发，下载实际完整私有字节且核membership，history/context/recovery
不消费cursor/renewal，worker身份/任务epoch被校验。上传进程在测试中
注入，另核最小state走stdin、不进argv、不改原账号；**不是新路线真实
CDN上传或手机收件**。相关入口/构建组一并通过，host3.6语法检查通过。

Root仍需安装和用既有单一authority做小量实际验收：先无副作用status/
context/history，再由本人明确测试文字/图片/文件与原ID读回；CLI返回
accepted、本人手机收件、附件实际可读分别记，不能让新sender回执冒充
已验收远端media。网络/额度/GUI等待不阻止其他独立mesh叶任务推进。
