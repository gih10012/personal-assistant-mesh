# 可选 DNS hostname TLS 兼容叶

负责人 `/root/chat_discovery_review_20261019`，分支
`work/hostname-tls-compat-20261019`，worktree 同名；base `443ad99`。
属于现 Goal 的普通 Chat 公网入口兼容工作。Root 独审/合入后独占运维；
本叶没有 SSH、生产、GUI、模型、证书签发、账号注册或凭据读取操作。
不改 Root 的 GOAL/PLAN/TASKS，不将换主机名解释为已证明原故障禁 IP。

新增 [addon renderer](../deploy/prepare-hostname-tls.py)，原
`prepare-ip-tls.py` 逐字节保留。无 `dns_profile` 或值为 null 时，所有输出
与原 IP renderer 相等。启用后在同一 nginx 容器追加精确 hostname vhost，
保留 IP vhost、证书、原 IP 12h timer/renew.sh/deploy-hook.sh。
MCP/OAuth/cloud 仍仅原窄路径；Header、body、Cookie、Host、no-retry
规则复用原 renderer，不开放 `/v1` 或 HTTP 登录路径。

## 私有配置与状态

[示例](../deploy/hostname-tls-owner.json.example) 中原 IP/端口/runtime_root
必须沿用实际 owner 配置。`dns_profile` 固定小写 ASCII FQDN、独立 mcp/
cloud 端口和同一 OAuth/MCP 端口；禁止 IP literal、wildcard、URL、端口、
尾点、空白及未知字段。端口不得碰管理端口或原 IP 侧车。cloud_port 为
null 时其窄路由仍 fail closed；配置端口也不代表后端实际准备就绪。

DNS 使用 `mesh-dns`，不扩展/替换原 `mesh-ip`，不沿用 IP-only 的
`--ip-address` 或强制 shortlived profile。Certbot 的 config/renewal/live/
archive、work、log 分别位于 runtime_root 下的：

```text
letsencrypt-dns-staging/       work-dns-staging/       logs-dns-staging/
letsencrypt-dns-production/    work-dns-production/    logs-dns-production/
```

HTTP-01 共用既有 webroot。仅既有 ACME account 目录按对应 CA 只读挂载，
不复制或打印账号私钥：`accounts.staging/production` 填原已存在的32位
小写十六进制 account ID（目录名是元数据），命令显式 `--account`。
没有注册子命令、`--agree-tos` 或无邮箱注册参数。缺账号保持失败，不能
为了检查注册一个新账号。生产 renew dry-run 显式选 staging server、
staging account ID 和原 staging accounts 只读目录，证书/renewal 状态
仍选 DNS production；Root 须以原固定 Certbot 镜像实际验证这条命令。

网关继续仅挂载证书 live/archive，不挂载 ACME accounts。额外两个只读
mount 的容器目标为 `/etc/letsencrypt-dns/live` 与 `.../archive`。
**现运行 TLS 容器没有这两个 mount：必须一次受控重建，reload 不能新增
mount。** 保留原 service/config/证书及恢复指令，不能把受控 restart
写成原 PID 不变。receiver/Worker/Claw 不因此重启。

## Root 最短步骤：staging → production

1. 核 hostname 的 A/AAAA/CAA、80/443 可达性及现有两个 ACME account
   元数据；保留原配置/service/证书清单。hostname 应只解析到已授权 VPS，
   无 AAAA 时不要补造 IPv6。用实际 cfg 和已审源码生成**新**目录：

   ```bash
   PYTHONPATH=/PRIVATE/REVIEWED_SOURCE PYTHONDONTWRITEBYTECODE=1 \
     python3 -B /PRIVATE/REVIEWED_SOURCE/deploy/prepare-hostname-tls.py \
     --owner-config /PRIVATE/HOSTNAME_OWNER.json --destination /PRIVATE/NEW_ASSETS
   ```

2. 审 `plan.json` 的 argv；创建其私有目录、安装新 DNS 脚本和已有 hook。
   初始 active config 选 `nginx-dns-bootstrap.conf`：原 IP HTTPS 加 DNS
   的80端口 challenge-only，不引用尚不存在的 DNS 证书。用原固定镜像
   `test_gateway_current_config` 验证后，安装含额外 DNS mounts 的原 TLS
   service，daemon-reload 并**受控重建这一 TLS 容器**。核 IP 原路由/
   证书与原续期配置保留，DNS 80 只有精确 challenge，未知 Host/路径拒绝。
   `nginx-current.conf` 也是此 bootstrap 模板；后续不要批量重装模板
   覆盖已经启用的双 TLS active config。
3. 先执行 `dns_issue_staging` argv；成功后仅执行一次
   `dns_issue_production`。不执行原 plan 的 IP issue 命令，不 force-renew，
   不把 staging cert 装进 HTTPS。Root 核实际 DNS production cert 的 SAN、
   链、有效期和私有权限；它与旧 IP lineage 分开保存。
4. Root 将 DNS 的 MCP/OAuth 侧车设为独立 loopback 端口/私有状态，
   canonical resource=`https://HOST/mcp`、issuer=`https://HOST/`；metadata、
   endpoints、JWT audience、challenge、Host 校验全部一致。原 IP 侧车
   和 issuer 留在原端口，不用 nginx 改写 metadata。若启用 DNS cloud，
   其后端 callback_url/Host 也必须独立匹配；不能直接把 IP-bound 后端
   当作 DNS-ready。所有私有密钥、GitHub App 配置由 Root 正常接线。
5. 切 active 为 `nginx-tls.conf` 的双 TLS 内容，`nginx -t` 成功再 reload。
   独立外部核 DNS SAN/公信链、两 metadata200、匿名 MCP401及正确
   challenge、`/v1/status`404，并复核原 IP；随后走原 Chat 表单正常发现/
   OAuth。原 private plugin ID/name 保留，不以建新插件绕过失败。

现有专用个人 GitHub OAuth App 可增加精确
`https://HOST/auth/callback`，关闭 wildcard matching，并保留原 IP
callback 便于并存。上游 App/secret、固定本人身份复用；ChatGPT 的
`https://chatgpt.com/connector_platform_oauth_redirect` 与 CIMD client ID
保持原值。实际 issuer identification 条件仍须满足，不降级来换 callback。

## 续期和回退

Root 先执行 `dns_dry_run_production_renewal`，成功后安装/启用独立
`personal-assistant-mesh-dns-renew.service/timer`。检查仍为12h并带 jitter；
Certbot 容器名/state/marker 均与 IP 分开。`renew-dns.sh` 只执行该 lineage
renew，看到 root:0600 marker 后对**现有 active config** test/reload；
不复制任何 nginx 模板。原 IP renew.sh 也不变，所以两者都不会把
双 host active config 替换为旧单 IP 配置。hook/test/reload 失败会保留
marker，不因此重复签发。

最小回退是 test/reload `nginx-dns-bootstrap.conf`：IP HTTPS/续期仍在，
DNS 80 challenge 仍在，DNS renew 可以保留；原私有 IP plugin/sidecar
配置可继续使用。DNS 侧车可独立关闭，不撤销或删除任何 lineage。
若要恢复**逐字节原 nginx config**，先停 DNS timer（保留其状态/证书），
再按原备份 test/reload；需要去掉新增 mount 时另做受控 TLS 容器重建。
不删原 IP 证书、不回滚账本，不重放任何 OAuth/task/launch unknown。

## 有限检查与依据

本机 Python3.14.7 上4项纯 renderer/argv 小组通过，零 skip；包含缺省
完整产物相等、双 vhost/认证边界、独立 ACME 命令、shell `-n`、新目录
写入及3.6 grammar parse。首次有两个 mount 字段断言误写成 target，
已按原 helper 实际 `dst` 键修正；没有 Docker/CA/生产调用。grammar
不是宿主3.6.8运行；真实固定 nginx/Certbot 镜像、DNS/CA/账号只读挂载
与续期、原 Chat 发现兼容是否改善均仍待 Root 实证。

- [sslip.io/nip.io 官方用法与 TLS](https://nip.io/) 支持该解析形式和单
  hostname HTTP-01；共享 DNS/封禁/CA额度仍是外部依赖，不支持 wildcard。
- [LE HTTP-01](https://letsencrypt.org/docs/challenge-types/) 要求80端口；
  [rate limits](https://letsencrypt.org/docs/rate-limits/) 按 PSL 判断 registered
  domain，不能把每个 IP hostname 称作独立免费配额。
- [Certbot](https://eff-certbot.readthedocs.io/en/stable/using.html) 的独立
  cert-name、webroot、staging/dry-run 与 renew/deploy-hook。
- [GitHub OAuth App](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app)
  当前可有多个 callback；[精确匹配](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps#redirect-urls)
  由关闭 wildcard matching 保持。
