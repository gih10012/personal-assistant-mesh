# 官方 Cloud 原环境/原任务 → Mesh：网络与凭据交接

2026-10-19，文档叶基线 `2d64431`。按 OpenAI Docs 技能先 search、再实际
fetch 当前官方页面；未用历史安装日志证明能力，未操作账号/UI/生产、
调用模型或提交 Cloud job。本叶不改变 Goal/Plan/Tasks、原操作或unknown。

结论：当前官方 Cloud 支持目的地限定的 HTTPS443 Network secret 占位符，
也支持编辑并重新发布**原环境**。但 Republish 不是给原运行任务热注入
新文件/配置的保证；本轮不能为验证而新建环境或代发新任务。

## 官方字段与适用范围

| 位置 | 当前官方字段 | 本项目最小值/边界 |
| --- | --- | --- |
| 环境网络 | Allow Codex to access internet | 打开环境网络访问；不等于取得Mesh权限 |
| 目的地 | Allow domains / Custom domains only / Additional allowed domains | 精确加入 `47-84-73-177.sslip.io`，不填写URL或整个共享父域 |
| Network secrets | Manage → Key | owner选定的专用Key；不是官方固定变量名，不与direct variable重名 |
| Network secrets | Value | Root已授权的有限scope Mesh credential；不是OpenAI模型key、GitHub或operator bearer |
| Network secrets | Allowed domains | 同一精确hostname；请求使用 `https://47-84-73-177.sslip.io/cloud/node` 的443端口 |

官方说明：程序接触占位符，代理仅在允许目的地替换实际凭据，setup和
task阶段都支持；原始secret不放入本地进程/文件。保存environment-owned
network secret会把目标加入restricted网络；direct变量/personal值不自动
加目的地。Personal vault只有环境请求的Key才交给task，还要匹配
Applies to。最短路径是原私有环境的Network secrets，并复核保存后的域策略。
[字段与投递](https://learn.chatgpt.com/docs/environments/cloud-environments#configure-environment-variables-and-network-secrets)、
[目的地网络](https://learn.chatgpt.com/docs/environments/cloud-environments#connect-to-services)。

`/docs/cloud/internet-access` 当前标题明确是 **Legacy**，其中旧的
GET/HEAD/OPTIONS限制不能直接当作当前published环境规则；若本人实际
UI是旧体验，需单独辨明，Mesh POST不能被只读method策略阻断。
[官方Legacy范围提示](https://learn.chatgpt.com/docs/cloud/internet-access)。

## 原环境更新与原任务边界

owner在 Settings → Codex Cloud → Environments，选原环境的 … → Edit，
修改/保存所需网络及secret设置；需要发布更新时用 Republish，不用Create。
官方流程对新prepared filesystem要求后续新task使用，已有task保留自己的
文件、未提交修改和工具。文档所说部分设置立即作用于active setup，不是
承诺已运行task获得新增secret/域权限。故本轮只验证原task已经可用的
Key/网络；缺失就报告，不能把Republish成功当其已热更新，也不代按
Start a new task。setup准备/测试可能涉及模型执行，留给本人正常操作。
[保存状态与更新](https://learn.chatgpt.com/docs/environments/cloud-environments#reuse-and-update-saved-state)。

本人可跨设备重开**同一原task**继续；官方Cloud可在电脑休眠时运行。
不能据环境发布推断本机skills/login已带入，也不向其复制认证目录；不能用Alibaba内部
`cloud`节点或本机代理代替该官方任务。
[原task续接](https://learn.chatgpt.com/docs/environments/cloud-environments#continue-on-web-or-mobile)。

## 最短原task操作：先确认现有投递，再发一次hello

1. Root私有交付真实 `CloudEntrance.bootstrap(grant_id)`，另附指定的
   `network_secret_key` 和原 `hello_request_id`。通过原task内容/附件/已有
   文件交付，保留账号隐私；不写公开repo，不猜一个bootstrap下载路由。
   不把仓库任意同名文件当Root交付；指定Key只对应Mesh专用credential。
   当前客户端的bootstrap来自该文件，CloudHTTPClient只负责认证POST。
2. owner原task检查指定Key是否存在、目标域及CloudHTTPClient是否可用，
   只报告存在/缺失，不打印环境变量、placeholder或credential。官方主文
   没有给placeholder固定字面值、KEY的代码读取示例或逐header替换schema；
   下面同名环境变量读取/Bearer header是本项目适配候选，需原task实证。
3. 保留Cloud原生代理和CA，使用既有CloudHTTPClient的标准HTTPS、拒绝
   redirect和一次请求；不加 `--noproxy`/关闭TLS校验。以下代码仅供owner
   在原任务使用，**本叶没有执行**：

```python
import json, os
from assistant_mesh.cloud_http import CloudHTTPClient

with open('mesh-cloud-handoff.json', encoding='utf8') as source:
    handoff = json.load(source)
bootstrap = handoff['bootstrap']
placeholder = os.environ.get(handoff['network_secret_key'])
if not placeholder:
    raise SystemExit('current_task_missing_network_secret_placeholder')
client = CloudHTTPClient(bootstrap['callback_url'], placeholder)
reply = client.request({
    'format': bootstrap['request_format'],
    'entrance_id': bootstrap['entrance_id'],
    'node_id': bootstrap['node_id'],
    'request_id': handoff['hello_request_id'],
    'action': 'hello', 'arguments': {},
})
print({'hello_received': reply.get('data', {}).get('authenticated_hello_received'),
       'provider_identity_verified': reply.get('provider_identity_verified')})
```

标准代理/CA、无redirect/无重试是[本仓库客户端](../assistant_mesh/cloud_http.py)
行为；官方的代理排障也明确保留环境代理。
[官方代理/网络排障](https://learn.chatgpt.com/docs/environments/cloud-environments#troubleshoot-setup-and-runtime-access)。

给本人原task的最短话术：读取Root交付的非秘密handoff，用指定Network
secret占位符和原ID从当前官方Cloud任务主动hello上述Mesh。缺代码/
Key/网络时只报具体缺项，保留本任务；不创建环境、另发Cloud job或复制
本机认证。连接后沿原Mesh request/conversation/message继续问答，
pending回复按write-scope sync推进，unknown只查原status/receipt。

## 真实待验项

- 原task是否实际拿到该Key/目的地权限；官方未保证原task热注入。若缺项，
  由本人决定后续正常操作，本轮不自动换任务验证。
- 占位符在该task的实际投递形式、目的地代理对Bearer header的替换；
  官方概念支持不等于已核这个账号/环境/任务的具体行为。
- Root专属Cloud ingress peer、有限期grant、`/cloud/node`侧车及hash匹配；
  公网DNS/MCP可用不代替Cloud路径已准备。bootstrap不能是占位身份。
- 真正hello、原问题/回复/receipt与完整成果需从原官方任务实际工具输出
  核证；owner授权/消息保存仍不等于provider attestation或模型执行。
  客户端关闭后的主动唤醒仍延后。
