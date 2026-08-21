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

编辑 `deploy/.env.local`，替换所有占位值。然后启动基础服务、API 和 Worker：

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

Compose 使用已通过 `npm --prefix admin run build` 生成的 `admin/dist` 构建 Python 镜像；运行时仍由 `deploy/.env.local` 注入品牌与服务配置。修改 Admin 源码后应先重新执行构建并提交对应产物。

## 混合检索升级

`QDRANT_COLLECTION` 默认使用 `knowledge_chunks_hybrid_v1`，其中同时保存 dense 与 `lexical` sparse vector。旧的单向量 collection 不能原地升级：保留旧 collection，将环境变量切换到新名称后，在 Admin 的“知识库文档”执行“重建全部索引”，待任务完成后再切换流量。不要删除旧 collection，直到抽样验证检索、引用和权限过滤。

## Kubernetes

1. 编辑 `deploy/k8s/app.yaml` 中的 ConfigMap，配置外部 PostgreSQL、Qdrant、Redis、Kafka、S3 兼容存储、模型服务、OIDC 和品牌。
2. 编辑 `deploy/k8s/kustomization.yaml`，设置镜像仓库与不可变版本号。
3. 复制并填写 Secret；本地 Secret 文件不会进入 Git。

```bash
cp deploy/k8s/secret.example.yaml deploy/k8s/secret.local.yaml
deploy/appctl k8s-check
deploy/appctl k8s-apply
```

清单部署 API、Worker、迁移 init container 和 ClusterIP Service。数据库、向量库、缓存、消息队列和对象存储应使用所在环境已有的高可用服务。入口域名、TLS 和网关策略由集群基础设施配置。

## 上线检查

- 不提交 `deploy/.env.local` 或 `deploy/k8s/*.local.yaml`。
- `AUTH_MODE=oidc` 时使用至少 32 字符的会话密钥并保持 `IDENTITY_COOKIE_SECURE=true`。
- 首次 OIDC 安装还必须在 Secret 中配置 `LOCAL_ADMIN_PASSWORD`，并在 ConfigMap 中配置 `LOCAL_ADMIN_USERNAME`。该唯一系统账号仅用于进入 `/login/admin` 分配 IdP 员工账号的应用权限；不要将其作为员工账号或 IdP 账号管理。
- Kubernetes 镜像使用不可变 tag 或 digest，不使用 `latest`。
- 部署后检查 `/live`、`/health`、`/`、`/login`、`/home`、`/admin/` 和 `/brand.js`。
- 分别验证登录、会话过期、401、403、退出和回跳路径。
