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
IDENTITY_POST_LOGOUT_REDIRECT_URI=https://<sn-knowledge-public-host>/ui/
IDENTITY_DEFAULT_TENANT_ID=<本应用租户 ID>
IDENTITY_SESSION_SECRET=<至少 32 字符随机值>
IDENTITY_COOKIE_SECURE=true
```

开发环境可以使用已登记的 `http://127.0.0.1:8000/auth/callback`，但不能把开发 Client、回调或 secret 用到测试/生产。应用启动入口为 `/auth/login`，登录成功默认回到 `/ui/`；`/account` 跳转到 IdP 个人中心，个人中心的“我的应用”会列出当前 subject 已授权的应用并可返回本应用。

## 安全约束

- state、nonce 和 PKCE verifier 存在短期 HttpOnly、SameSite=Lax Cookie；回调必须校验 state、nonce、issuer、audience、签名和时间。
- 浏览器只得到签名的短期应用会话 Cookie；access token、refresh token、授权码和 client secret 不进入浏览器 localStorage、日志或模型上下文。
- `sub` 是稳定身份键。应用角色从本地数据库 `principal_roles`/`roles` 读取，绝不信任 IdP token 中的 roles。
- `/auth/login` 的 `next` 只接受本站相对路径，应用入口和 callback 由 IdP 服务端登记，不接受请求参数覆盖。

在 OpenIdentity 管理台创建 `sn_knowledge` 的每环境 Application/Client 后，必须登记实际公开域名的精确 callback；当前仓库没有假定生产域名，因此正式注册和 IDC 部署需以该地址为准。

生产上线前还必须完成 OpenIdentity 的生产验收：以 discovery 返回的 issuer、JWKS、授权、令牌和注销端点为准，验证登录、过期、401、403、回跳和 back-channel logout/revocation。当前 `open-identity` 仓库自身标记为 NO-GO，故本应用已完成接入契约和配置边界，但不能据此宣称生产身份认证已验收。

## 未完成记录

- [x] 应用侧 OIDC Authorization Code + PKCE、state/nonce、JWT 签名、issuer、audience 和本地角色映射。
- [x] 注销使用 discovery 的 `end_session_endpoint`，并在 IdP 不可用时清理本地会话。
- [ ] 由 OpenIdentity 平台提供正式生产 issuer、client ID/secret、租户 ID，并登记精确 callback/logout 回跳地址。
- [ ] 完成真实生产登录态验收：登录成功、会话过期、401、403、失败回跳和权限拒绝。
- [ ] 按 OpenIdentity 生产契约补齐并验收 `id_token_hint`、back-channel logout/revocation 和密钥轮换。
- [ ] OpenIdentity 从 NO-GO 变为可上线状态后，再执行 IDC 部署和不可变镜像验收。
