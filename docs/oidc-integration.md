# sn_knowledge 接入 OpenIdentity

`sn_knowledge` 支持本地用户名密码和 OpenIdentity OIDC Authorization Code + PKCE 两种登录方式。知识内容必须登录后使用；公开自助注册关闭，只有应用管理员可以创建本地用户或预先准入 IdP 的稳定 subject。应用不处理飞书等企业 Provider，也不调用 Keycloak Admin API。

本地密码属于知识库自身账号，绝不作为 IdP 密码使用；IdP 登录不使用 password grant，不按邮箱、姓名或用户名自动合并身份。此处本地用户能力来自最新产品要求，取代旧的“仅初始化管理员有本地密码”限制。

## 登录模式与用户管理

- `AUTH_MODE=local`：仅本地用户名密码与应用 Cookie；不接受 IdP 身份或机器 Bearer。新配置模板默认使用此模式。
- `AUTH_MODE=oidc`：同一登录页提供本地登录和企业 IdP 登录，初始化管理员也使用本地登录。
- `AUTH_MODE=dev` / `jwt`：仅显式启用的隔离开发/评测模式，不可作为正式用户部署模式。
- `/login` 没有注册入口；管理员在“成员与权限”中创建本地账号或添加 IdP `sub`。未知 IdP subject 不会在回调或请求期间自动建号。
- 本地用户可通过 `/account` 修改自己的密码；管理员可重置普通本地用户密码。IdP 账号仍前往 IdP 维护资料/密码。
- 修改/重置密码以及禁用账号会递增应用会话版本；旧 Cookie 失效。初始化管理员不能由成员管理删除权限、禁用或重置密码，但可自行改密。
- 本地登录有独立 Redis 限流；Redis 不可用时拒绝登录，不绕过保护。登录、改密和成员管理写操作检查同源请求，不应通过关闭安全检查解决反向代理的 origin 配置错误。其余历史写接口的统一 CSRF 覆盖另见 `AUTH-07`。

接口：`POST /auth/local/login`、`POST /api/v1/admin/members`、`POST /api/v1/admin/members/{subject}/password`、`POST /api/v1/account/password`。没有匿名注册接口；添加 IdP subject 仅登记本应用准入，不会在 IdP 创建企业账号。

## 配置

生产环境设置：

```env
AUTH_MODE=oidc
IDENTITY_ISSUER=https://<openidentity-public-host>/realms/openidentity
IDENTITY_CLIENT_ID=<OpenIdentity 为 sn_knowledge 下发的环境 Client ID>
IDENTITY_CLIENT_SECRET=<仅服务端保存>
IDENTITY_REDIRECT_URI=https://<sn-knowledge-public-host>/auth/callback
IDENTITY_POST_LOGOUT_REDIRECT_URI=https://<sn-knowledge-public-host>/
IDENTITY_DEFAULT_TENANT_ID=<本应用租户 ID>
IDENTITY_SESSION_SECRET=<至少 32 字符随机值>
IDENTITY_COOKIE_SECURE=true
LOCAL_ADMIN_USERNAME=<安装时创建的唯一本地管理员用户名>
LOCAL_ADMIN_PASSWORD=<至少 16 字符，仅保存在部署 Secret 中>
```

访问 `/` 时，只有签名、会话版本、账号类型、租户与账号启用状态均有效才跳转 `/home`，否则进入 `/login`。IdP 登录由 `/auth/login` 发起，登录成功默认回到 `/home`；本地密码提交到 `/auth/local/login`。`/account` 根据真实账号来源显示本地改密页或跳转 IdP 个人中心。

## 安全约束

- state、nonce 和 PKCE verifier 存在短期 HttpOnly、SameSite=Lax Cookie；回调必须校验 state、nonce、issuer、audience、签名和时间。
- 浏览器只得到签名的短期应用会话 Cookie；access token、refresh token、授权码和 client secret 不进入浏览器 localStorage、日志或模型上下文。
- `sub` 是稳定身份键。应用角色从本地数据库 `principal_roles`/`roles` 读取，绝不信任 IdP token 中的 roles。
- `/auth/login` 的 `next` 只接受本站相对路径，应用入口和 callback 由 IdP 服务端登记，不接受请求参数覆盖。

## 机器调用与 Service Account

生产 `AUTH_MODE=oidc` 也接受机器调用的 OIDC access token：

```http
Authorization: Bearer <OIDC access token>
```

服务端从 discovery 的 `jwks_uri` 取得签名密钥，并要求 token 的签名算法、`iss`、`aud`、`sub` 和未过期的 `exp` 均有效；`aud` 使用现有的 `IDENTITY_CLIENT_ID`。无效 bearer 不会退回使用浏览器 session cookie。

机器身份必须预先写入本应用的 `principals`，且 `tenant_id` 必须等于 `IDENTITY_DEFAULT_TENANT_ID`、`status=active`、`principal_type=service_account`。再由本应用管理员通过现有“成员与权限”接口为该 subject 分配角色；机器 token 中的 `roles`、`tenant_id` 等字段不授予权限，服务端每次请求都从 `principal_roles`/`roles` 重新读取当前角色。因此禁用 service account 或移除角色会立即影响后续请求。

`sub` 是 OpenIdentity client-credentials token 中的稳定 service-account subject，必须用 token 实际返回的值建记录。示例（由部署管理员执行一次，具体数据库连接由部署方式提供）：

```sql
INSERT INTO principals (id, tenant_id, display_name, principal_type, status)
VALUES ('<token-sub>', '<IDENTITY_DEFAULT_TENANT_ID>', 'Knowledge API worker', 'service_account', 'active');
```

然后使用已登录的本应用管理员调用 `PUT /api/v1/admin/members/<token-sub>/roles` 分配最小角色。客户端通过 discovery 返回的 `token_endpoint` 使用 `grant_type=client_credentials` 获取 access token，并在调用 API 时发送上述 header；client secret 只放在机器的 Secret 中，不放入代码、日志或请求 URL。

## 应用管理员与 IdP 员工账号

首次以 `AUTH_MODE=local` 或 `oidc` 启动时，服务使用 `LOCAL_ADMIN_USERNAME` 与 `LOCAL_ADMIN_PASSWORD` 创建唯一的本地初始化管理员；凭据仅在首次创建时读取并以哈希保存，之后修改环境变量不会重置账号或密码。必须替换安装密码占位值。旧 `/login/admin` 入口保留兼容；普通登录页也支持该账号。

- 本地初始化管理员不是 IdP 用户：不承载 IdP 资料；初始化后可通过本地改密流程维护密码。
- Admin 的“成员与权限”管理本地用户及预准入 IdP 用户的应用角色和访问状态。IdP 的姓名、邮箱、密码和个人资料仍只能在 IdP 管理。
- 管理员不能修改自己的访问状态或角色；禁用 IdP 账号后，服务端会拒绝其后续 API 请求。

在 OpenIdentity 管理台创建 `sn_knowledge` 的每环境 Application/Client 后，必须登记实际公开域名的精确 callback；当前仓库没有假定生产域名，因此正式注册和 IDC 部署需以该地址为准。

生产上线前还必须完成 OpenIdentity 的生产验收：以 discovery 返回的 issuer、JWKS、授权、令牌和注销端点为准，验证登录、过期、401、403、回跳和 back-channel logout/revocation。源码与本地合成测试不证明外部 IdP 已生产验收；本项目不据旧文档推断平台当前上线状态。

接入依据是 OpenIdentity 的 `docs/application-oidc-integration.md` 与 `docs/api-service-integration.md`。后者要求独立机器 Client，以及精确 `typ=Bearer`、`aud`、`azp`、`tenant_id`、接口 scope 校验。当前既有机器入口的基础验签/本地角色映射不能代替这套完整契约，补齐与联调跟踪于 `M2M-01`。不要为本项目新增 API key 体系，也不要把员工 Client 的授权复制给机器 Client。

## 未完成记录

- [x] 应用侧 OIDC Authorization Code + PKCE、state/nonce、JWT 签名、issuer、audience 和本地角色映射。
- [x] OIDC bearer access token 的 issuer、audience、JWKS 签名和 expiry 校验，以及已显式配置 service account 的数据库角色映射。
- [x] 注销使用 discovery 的 `end_session_endpoint`，并在 IdP 不可用时清理本地会话。
- [ ] 由 OpenIdentity 平台提供正式生产 issuer、client ID/secret、租户 ID，并登记精确 callback/logout 回跳地址。
- [ ] 完成真实生产登录态验收：登录成功、会话过期、401、403、失败回跳和权限拒绝。
- [ ] 按 OpenIdentity 生产契约补齐并验收 `id_token_hint`、back-channel logout/revocation 和密钥轮换。
- [ ] 完成新 API Service 严格 claims/scope 接入与真实机器客户端验收。
- [ ] 获得明确部署授权后，再执行 IDC 部署和不可变镜像验收。
