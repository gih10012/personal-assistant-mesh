# 官方 Cloud 到 Mesh 的窄接入通路

2026-10-13：本人报告已经发布环境。发布和真实任务/入网分开核验。
本片新增 [cloud_http.py](../assistant_mesh/cloud_http.py)：将既有
CloudMembership 接到单一 JSON HTTP route，并提供一次 HTTPS client。
没有创建生产 listener、注册节点、申请证书、复制凭据或提交 Cloud job。

## 当前实机门槛

个人 VPS 的 Mesh 仅监听 loopback。只读核对没有 80/443 listener、
Tailscale binary/state/socket、ACME 目录或已配置的本人 HTTPS 域名。
现存面板/订阅是 HTTP；两张证书都是自签名，其中一张还是旧 IP。
Xray Reality 的外部伪装域名不是本人拥有的域名或证书。不能复用这些
身份成为公信 HTTPS，也不能把完整 operator bearer 发到 Cloud。

这是本次检查的本机/VPS 配置范围，不声称本人在别处没有域名或 VPN。
个人或组织身份的拥有权须单独验证，不能自动借用 EaseCation 身份。

## 三条可落地路线

1. **已有个人域名**：本人授权 DNS 指向和有效 TLS 证书，固定一个 HTTPS
   443 入口，仅反向代理 `/cloud/node` 到窄 facade，原 Mesh API 保持私有。
2. **已有公网 IP 的公信证书**：不必购买域名。Let's Encrypt 已开放
   IP 地址的短期证书；Certbot 5.4+ 支持相应 webroot 申请，需可达的
   HTTP-01 challenge、自动续期及证书 reload hook。160 小时有效期不是
   永久可用性。申请 CA 身份/TLS key 与新增公开监听需要本人许可，
   本片没有执行；Cloud 是否接受 IP 网络秘密/允许目标仍须真任务验证。
   [IP certificate availability](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability)，
   [Certbot support](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)。
3. **Tailscale 私网**：复用本人已经授权的 tailnet，或先由本人完成登录。
   官方 Cloud 环境 VPN 支持 Tailscale；网络策略和 VPN ACL 都要允许
   相应目标，配置变更后 republish/new task 实测。当前机器未找到该
   已有配置，不能假造其存在。现 CloudMembership callback 仍要求
   HTTPS:443，VPN 可达不自动放宽此合同。

Cloud 的网络秘密只在 HTTPS:443 的获批目的地替换 placeholder；环境
允许联网不提供 Mesh 身份，VPN 也不提供任务授权。使用原生代理环境，
不使用 `--noproxy` 绕开 Cloud 的 VPN 代理。以上依据已实际 fetch 的
[官方 Cloud environment 文档](https://learn.chatgpt.com/docs/environments/cloud-environments)。

## 最小接线与实测顺序

- Trusted owner 核实真实 published environment 和持久 Cloud task receipt。
  现 CloudJobs 只接受旧版 `/codex/tasks/<id>`；新版 `/local/*` 在确认真实
  持久身份前不能放宽为任意 URL，尤其不能认领临时 `client-new-thread`。
- 按原 Mesh task/epoch/lease 配置已有 CloudMembership，独立 task-bound
  节点凭据仅以 hash 进入 journal，值通过本人正常 Cloud secret 管理注入。
  不把 OAuth/account home 或 operator token 当 bootstrap。
- Trusted installer 创建 handler，将它绑定到一个私有 loopback listener，
  再接到既有可信 TLS terminator；必须 preserve Host、覆盖而非追加
  `X-Forwarded-Proto: https`。仅显式 loopback peer 可以断言已终止 TLS。
  公网直连 plain HTTP 即使伪造此 header 仍拒绝；不公开整个 `/v1/*`。
- 在真 VM 通过 `hello`、获授能力 `discover/call`、完整 artifact 和 report
  验收。停止 VM 后 owner 从 Mesh journal 读取完整字节并独立核对。
  只有 status/HTTP200/hash/provider 自报不证明独立执行或整体入网。

## 运输边界

只支持固定 canonical resource path 的 POST；拒绝 query/编码别名、
不同 Host、重复 Authorization/Content-Length、chunked/compressed body、
cookie/origin、重复 JSON key、NaN/非法 UTF-8/surrogate。默认 body 8 MiB，
可显式配置到 24 MiB，涵盖 16 MiB 二进制的 base64 开销；更大成果另扩展。
正常 HTTPS client 校验 CA 与 hostname、拒绝 redirect、最多一次网络调用，
保留 native Cloud proxy，不设置宿主代理或拦截原生工具。

facade 之后的任何异常都保守标为 unconfirmed，错误不含 token/prompt。
原 request 的 unknown 查询只读原 journal，不换 ID 或重放业务。SDK/MCP
OAuth 的普通 Chat 入网另接 [Chat MCP](CHAT-MCP-HTTP.md)，不以临时 Cloud
节点凭据替代 Chat 用户的 OAuth 同意。

本机新增9项真实 loopback运输测试，相关 Cloud 共71项通过；有固定
task-bound journal/full153600B artifact/12000行结果离线读回、错误身份和
模拟失回执防重放。provider CLI 为 fake、TLS terminator 为显式合成
loopback前置条件，**不是公网 TLS/官方 VM 调用的证据**。
