# 部署与运行

所有运行方式使用环境变量配置。品牌名称、标记、说明、页脚、主色和 Logo 在进程启动时读取，同一镜像无需重新构建即可切换展示内容。

## 品牌配置

| 变量 | 说明 | 示例 |
|---|---|---|
| `BRAND_NAME` | 页面、Admin、API 文档显示的产品名称 | `Knowledge Workspace` |
| `BRAND_MARK` | 没有 Logo 时显示的 1–4 个字符 | `K` |
| `BRAND_TAGLINE` | 顶部副标题和浏览器标题说明 | `企业知识助手` |
| `BRAND_FOOTER` | 页脚短句；留空则隐藏 | `Evidence first` |
| `BRAND_PRIMARY_COLOR` | 六位十六进制主色 | `#4f5bd5` |
| `BRAND_LOGO_URL` | 站内绝对路径或 HTTP(S) 图片地址 | `/assets/logo.svg` |

配置通过 `/brand.js` 提供给主问答页和 Admin。该响应禁用缓存，重启 API 后刷新页面即可生效。

## 本机源码运行

要求：Python 3.12、Node.js 22、Docker、可访问的模型服务。

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
npm --prefix admin ci
npm --prefix admin run build
deploy/appctl init-source
```

编辑 `deploy/.env.local`，替换所有占位值，尤其首次安装的 `LOCAL_ADMIN_USERNAME` / `LOCAL_ADMIN_PASSWORD`。模板默认 `AUTH_MODE=local`，先用初始化管理员登录，再由管理员添加用户；没有公开注册入口。然后启动基础服务、API 和 Worker：

```bash
deploy/appctl infra-up
deploy/appctl local-api
# 另一个终端
deploy/appctl local-worker
```

访问 `http://127.0.0.1:8000/`、`http://127.0.0.1:8000/home` 和 `http://127.0.0.1:8000/admin/`。

需要复现项目评测时，在 API 和 Worker 已启动、`AUTH_MODE=dev` 的本地环境显式初始化测试资料：

```bash
deploy/appctl init-test-data
```

命令会校验仓库内四部公开测试文本的 SHA-256，将它们上传到“测试资料”知识库。该操作不会在生产启动时自动执行；其他端口可通过 `TEST_DATA_BASE_URL` 指定。

## Docker Compose

```bash
deploy/appctl init-docker
# 编辑 deploy/.env.local
deploy/appctl docker-up
deploy/appctl status
```

停止服务但保留数据卷：

```bash
deploy/appctl docker-down
```

Compose 使用已通过 `npm --prefix admin run build` 生成的 `admin/dist` 构建 Python 镜像；Python 基础镜像固定 digest，运行依赖由 `uv sync --frozen --no-dev --no-install-project` 按 `uv.lock` 安装；运行时仍由 `deploy/.env.local` 注入品牌与服务配置。修改 Admin 源码后应先重新执行构建并提交对应产物。

## PostgreSQL 18 / Kafka 4 升级边界

Compose 使用 PostgreSQL 18.6 和 Kafka 4.3.1；localidc 直接复用 infra 中同版本服务，不对共享实例执行升级。

PostgreSQL 18 的卷挂载点改为 `/var/lib/postgresql`，数据目录由官方镜像设为 `/var/lib/postgresql/18/docker`。保留原 `postgres_data` 卷名；旧 PG17 数据卷不能直接升级，官方启动脚本检测到旧数据会拒绝初始化。已有数据先备份，再经受控 `pg_upgrade` 或逻辑导出／导入迁移，验证后切换；不要删除原卷或执行 `down -v`。[官方镜像说明](https://hub.docker.com/_/postgres)、[旧数据检测实现](https://github.com/docker-library/postgres/blob/master/docker-entrypoint.sh)。

Kafka 使用单节点 KRaft 开发配置；项目 Compose 未配置 Kafka 持久卷。已有消息或自定义持久卷的环境，切换前必须单独确认保留／迁移方案，不能以开发配置重建 broker 代替迁移。共享 Kafka 不执行任何存储格式化或重建操作。

## 混合检索升级

`QDRANT_COLLECTION` 默认使用 `knowledge_chunks_hybrid_v1`，其中同时保存 dense 与 `lexical` sparse vector。旧的单向量 collection 不能原地升级：保留旧 collection，将环境变量切换到新名称后，在 Admin 的“知识库文档”执行“重建全部索引”，待任务完成后再切换流量。不要删除旧 collection，直到抽样验证检索、引用和权限过滤。

## Kubernetes

PostgreSQL、Redis、Qdrant、Kafka 和 S3 兼容存储统一由 base-plat 在 Kubernetes `infra` 分组／命名空间提供。本项目复用共享服务，不部署副本或修改共享基础设施。接入前实查运行大版本和驱动兼容性；缺少对应大版本或组件时先报告，再决定是否由 base-plat 安装。

2026-10-26 实查运行版本：PostgreSQL 16.15／18.6、Redis 7.4.11、Qdrant 1.15.4、Kafka 4.3.1、SeaweedFS 4.48。项目目标为 PostgreSQL 18 和 Kafka 4，Kubernetes 复用 `postgresql-18.infra.svc.cluster.local:5432`、`kafka-4.infra.svc.cluster.local:9092`，由平台提供应用专属数据库及最小权限凭据。Qdrant 1.13 客户端与服务端 1.15.4 的兼容性仍待验证。Compose 仅用于本机开发，不代表允许在 Kubernetes 安装共享组件副本。

1. 编辑 `deploy/k8s/app.yaml` 中的 ConfigMap，配置外部 PostgreSQL、Qdrant、Redis、Kafka、S3 兼容存储、模型服务、OIDC 和品牌。
2. 编辑 `deploy/k8s/kustomization.yaml`，设置镜像仓库与不可变版本号。
3. 复制并填写 Secret；本地 Secret 文件不会进入 Git。

```bash
cp deploy/k8s/secret.example.yaml deploy/k8s/secret.local.yaml
deploy/appctl k8s-check
deploy/appctl k8s-apply
```

应用 namespace 固定为 `knowledge`。清单部署 API、Worker、迁移 init container 和 ClusterIP Service。数据库、向量库、缓存、消息队列和对象存储应使用上述兼容的共享服务。入口域名、TLS 和网关策略由集群基础设施配置。localidc 应用部署和测试已获授权；生产部署及外部 IdP 注册仍需单独授权。

## 上线检查

- 不提交 `deploy/.env.local` 或 `deploy/k8s/*.local.yaml`。
- 正式用户环境选择 `AUTH_MODE=local` 或 `oidc`，不使用 dev/jwt。使用至少 32 字符的会话密钥，HTTPS 部署保持 `IDENTITY_COOKIE_SECURE=true`；已有环境文件不会被模板升级自动覆盖。
- 首次 local/OIDC 安装必须在 Secret 中配置 `LOCAL_ADMIN_PASSWORD`，并配置 `LOCAL_ADMIN_USERNAME`。之后修改环境变量不会重置既有管理员密码。初始化管理员可管理应用用户，但不代表任何 IdP 企业身份。
- OIDC Client 的 callback 与公开 origin 必须匹配实际反向代理入口；预准入 IdP subject 后才能登录。不要为了跨站请求通过而关闭同源防护。
- Kubernetes 镜像使用不可变 tag 或 digest，不使用 `latest`。
- 部署后检查 `/live`、`/health`、`/`、`/login`、`/home`、`/admin/` 和 `/brand.js`。
- 分别验证登录、会话过期、401、403、退出和回跳路径。

## 共享发号服务与数据库迁移

所有新业务主键、事件、审计和 outbox ID 均由 base-plat `global-id-service` 分配，调用方通过独立机器身份取得 `global-id.generate` 权限。部署注入 `GLOBAL_ID_URL`、`GLOBAL_ID_TOKEN_URL`、`GLOBAL_ID_CLIENT_ID`、`GLOBAL_ID_CLIENT_SECRET`；集群批准的私网 HTTP 额外显式设置 `GLOBAL_ID_ALLOW_HTTP=true`，HTTPS 仍校验证书。配置缺失、请求失败或非规范响应会停止业务写入，无本地发号兜底。

应用使用规范的正十进制 int64 **字符串**；数据库业务 ID 列限制为 19 字符并校验格式和范围，接口拒绝 UUID 业务 ID。Qdrant 的 point ID 使用同一 Chunk ID，并仅在 Qdrant 协议边界转换为整数。IdP subject、租户外部引用及固定安装键仍遵守各自契约。

升级旧数据前停止本应用所有写入并保留数据库、对象存储和索引恢复点。前向迁移统一向权威服务申请新 ID，事务内改写主外键、审计、反馈、事件及 outbox 引用；不会保留 UUID 运行时兼容分支。原件对象键作为不透明存储地址保留，避免重命名文件造成数据丢失。迁移后启用新的应用专属 Qdrant collection 和 Kafka topic，再重建索引，禁止继续消费旧 UUID 消息或查询旧索引。旧恢复点仅用于人工回退，不进入运行时查询。迁移不可直接 downgrade，需要从备份恢复。

localidc 候选清单位于 `deploy/localidc`，复用 `knowledge/knowledge-database` 的数据库连接，使用独立的新索引／topic；候选镜像已在本机构建；先验证目标节点并导入镜像，再完成其余 Secret、模型、存储及域名授权，不能直接应用尚有占位值的候选清单。

localidc 尚需完成专属 IdP 机器授权、模型服务及对象存储权限后才可完成上线验收。客户端注册必须使用现网 OpenIdentity 管理控制平面，不用安装脚本直接写 Keycloak，不借用其他应用客户端。
