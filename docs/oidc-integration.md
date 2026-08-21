# sn_knowledge 接入 OpenIdentity

`sn_knowledge` 的浏览器登录使用 OpenIdentity 的 OIDC Authorization Code + PKCE。应用不处理飞书等企业 Provider，也不调用 Keycloak Admin API。

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

访问 `/` 时，存在有效应用会话则跳转 `/home`，否则进入 `/login`。`/login` 只发起 `/auth/login`；生产环境由 OpenIdentity 完成认证，开发环境的同一按钮仅创建本地开发会话后进入 `/home`。应用启动入口为 `/auth/login`，登录成功默认回到 `/home`；`/account` 跳转到 IdP 个人中心，个人中心的“我的应用”会列出当前 subject 已授权的应用并可返回本应用。

## 安全约束

- state、nonce 和 PKCE verifier 存在短期 HttpOnly、SameSite=Lax Cookie；回调必须校验 state、nonce、issuer、audience、签名和时间。
- 浏览器只得到签名的短期应用会话 Cookie；access token、refresh token、授权码和 client secret 不进入浏览器 localStorage、日志或模型上下文。
- `sub` 是稳定身份键。应用角色从本地数据库 `principal_roles`/`roles` 读取，绝不信任 IdP token 中的 roles。
- `/auth/login` 的 `next` 只接受本站相对路径，应用入口和 callback 由 IdP 服务端登记，不接受请求参数覆盖。

## 应用管理员与 IdP 员工账号

首次以 `AUTH_MODE=oidc` 启动时，服务使用 `LOCAL_ADMIN_USERNAME` 与 `LOCAL_ADMIN_PASSWORD` 创建唯一的本地初始化管理员；凭据仅在首次创建时读取并以哈希保存，之后修改环境变量不会重置账号或密码。该账号从 `/login/admin` 进入 Admin，用于为已通过 IdP 准入的员工账号配置本应用角色和启用状态。

- 本地初始化管理员不是 IdP 用户：没有资料页、密码修改页或同步逻辑；密码只来自安装过程。
- Admin 的“成员与权限”只管理 IdP 账号的应用角色和本应用访问状态。IdP 的姓名、邮箱、密码和个人资料仍只能在 IdP 管理。
- 管理员不能修改自己的访问状态或角色；禁用 IdP 账号后，服务端会拒绝其后续 API 请求。

在 OpenIdentity 管理台创建 `sn_knowledge` 的每环境 Application/Client 后，必须登记实际公开域名的精确 callback；当前仓库没有假定生产域名，因此正式注册和 IDC 部署需以该地址为准。

生产上线前还必须完成 OpenIdentity 的生产验收：以 discovery 返回的 issuer、JWKS、授权、令牌和注销端点为准，验证登录、过期、401、403、回跳和 back-channel logout/revocation。当前 `open-identity` 仓库自身标记为 NO-GO，故本应用已完成接入契约和配置边界，但不能据此宣称生产身份认证已验收。

## 未完成记录

- [x] 应用侧 OIDC Authorization Code + PKCE、state/nonce、JWT 签名、issuer、audience 和本地角色映射。
- [x] 注销使用 discovery 的 `end_session_endpoint`，并在 IdP 不可用时清理本地会话。
- [ ] 由 OpenIdentity 平台提供正式生产 issuer、client ID/secret、租户 ID，并登记精确 callback/logout 回跳地址。
- [ ] 完成真实生产登录态验收：登录成功、会话过期、401、403、失败回跳和权限拒绝。
- [ ] 按 OpenIdentity 生产契约补齐并验收 `id_token_hint`、back-channel logout/revocation 和密钥轮换。
- [ ] OpenIdentity 从 NO-GO 变为可上线状态后，再执行 IDC 部署和不可变镜像验收。
