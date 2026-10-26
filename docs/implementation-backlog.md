# 未完成能力与逐步交付清单

更新日期：2026-10-26。状态只能由实现、审查和验证证据推进；历史测试通过不代表未实现功能已经完成。每个交付阶段单独提交，不自动推送或部署。

## 当前阶段：登录与受控用户创建

| ID | 任务 | 状态 | 验收要求 |
| --- | --- | --- | --- |
| AUTH-01 | 登录前禁止使用知识库 | 本地验收完成 | 页面跳转登录；内容/API 拒绝匿名、过期、禁用和未准入账号；管理员入口有服务端校验 |
| AUTH-02 | 本地用户名密码及初始化管理员登录 | 本地验收完成 | 密码哈希、统一错误、输入边界及防暴力尝试；安装过程不覆盖既有密码；本地与 IdP 身份分离 |
| AUTH-03 | 管理员创建用户、关闭公开注册 | 本地验收完成 | 仅管理员创建本地用户或准入 IdP subject；重复、跨租户、越权和身份冲突被拒绝；无匿名注册入口 |
| AUTH-04 | 本地密码维护与会话失效 | 本地验收完成 | 管理员重置/用户改密；旧会话失效；禁用与角色变更立即生效；不修改 IdP 密码 |
| AUTH-05 | OIDC 准入与双登录界面 | 本地验收完成 | Code + PKCE、state/nonce、签名/issuer/audience；未知 subject 不自动注册；不按邮箱合并；真实 HTTP 回调测试 |
| AUTH-06 | IdP 登出与撤销完整契约 | 待实现 | 受保护 id_token_hint、Back-Channel Logout/撤销事件、持久去重与会话范围撤销；真实 IdP 联调另行授权 |
| AUTH-07 | 全站写接口 CSRF 一致性 | 本地验收完成 | 将登录、改密和成员管理的同源保护推广至上传、替换、重建索引、别名等历史 Cookie 写接口；保留机器 Bearer 调用边界 |

最新用户要求取代旧设计中“仅初始化管理员允许本地密码”的限制。知识库可管理本地用户凭据，但不能收集或管理 IdP 企业账号密码。公开自助注册保持关闭。

## 后续交付阶段

| ID | 能力 | 状态 | 完成边界 |
| --- | --- | --- | --- |
| ACL-01 | 文档授权管理 | 本地验收完成 | 查看/修改授权 API 与 UI、事务审计、缓存失效及 Qdrant 权限同步 |
| ACL-02 | 操作级权限 | 待实现 | 将 Permission 接入操作授权，区分查询、上传、删除、用户管理等能力 |
| JOB-01 | 真实任务与死信中心 | 待实现 | 按真实任务/阶段/错误码分页查询、进度耗时、受控重试/终止、删除恢复 UI |
| M2M-01 | AI worker 应用与身份管理 | 待实现 | 应用绑定知识库和检索配置、机器 subject 准入；按新 IdP 契约验证独立机器 client 的 typ/aud/azp/tenant/scope |
| M2M-02 | 调用额度与请求幂等 | 待实现 | 每应用/worker 配额与 Token/费用统计、写入幂等键；复用现有共享限流，不新增 API key 身份体系 |
| KB-01 | 知识库管理与检索配置 | 待实现 | 知识库 CRUD、查询范围选择、RetrievalProfile 与 Application 管理 |
| DOC-01 | 历史版本生命周期 | 待实现 | 历史列表、指定版本查看、发布与回滚；不复活已撤权内容 |
| DOC-02 | Chunk 管理 | 待实现 | 预览/编辑/禁用/单块重嵌入、人工修订保护与原文位置 |
| ING-01 | 企业资料解析 | 部分完成 | 已有 PDF 文字/DOCX 段落/TXT/Markdown；补结构化表格、扫描 OCR、版面与结构分块 |
| ING-02 | 多入口资料接入 | 待实现 | URL、批量导入任务、增量同步；复用现有可靠入库链路，外部来源需明确范围 |
| RAG-01 | 检索增强 | 部分完成 | 已有混合检索；补 metadata 过滤、按评测启用 rewrite/rerank、邻近片段合并与上下文预算 |
| CHAT-01 | 流式问答及会话 | 待实现 | 安全流式输出、会话上下文/历史恢复、原始文件页或表格位置引用；保持证据授权 |
| LIFE-01 | 保留期及数据清理 | 待实现 | 回收站恢复、原件保留策略、审计归档、孤儿对象对账、旧版本清理死信闭环 |
| OBS-01 | 观测与审计闭环 | 部分完成 | 已有请求指标/部分审计；补跨组件 trace、阶段/模型用量、积压/DLQ 告警及可追溯答案证据 |
| QA-01 | 企业业务质量评测 | 待实现 | 公司业务合成/授权语料，表格/扫描/时效/权限反例；人工审阅绑定当前结果 |
| QA-02 | 持续验证与可复现交付 | 待实现 | CI、自动浏览器回归、镜像使用锁定依赖、安全检查；外部 CI 配置另行授权 |
| OPS-01 | 容量与灾备验收 | 待验证 | 1500 员工与 AI worker 混合压测、并发入库、积压恢复、备份恢复；明确流量及网关配额后在授权环境执行 |
| OPS-02 | 生产身份与部署验收 | 待验证 | 正式 IdP client/callback/Secret、真实登录及撤销、多副本与基础服务 HA；不自动修改外部系统 |
| OPS-03 | 共享基础设施兼容与验收 | 待验证 | 复用 base-plat 提供的共享服务；验证驱动/配置、迁移兼容性及应用连接。未发现 Knowledge 服务；缺失/不兼容项先报告，不在业务仓库部署副本或修改 infra。当前检查到 PostgreSQL 16.15/18.6、Redis 7.4.11、Qdrant 1.15.4、Kafka 4.3.1、SeaweedFS 4.48；Kubernetes 与 Compose 目标为 PostgreSQL 18 / Kafka 4；升级需保留既有数据，禁止直接以升级版本启动旧数据卷。Qdrant 1.13 client 对 1.15.4 server 的兼容性待验证 |
| ID-01 | 业务 ID authority | 代码、数据库、平台登记与真实 SDK 取号通过；应用链路待部署 | 所有新持久化业务 ID 通过共享 `global-id-service`；验证调用方、并发分配、失败拒绝写入和重放边界。服务位于 `sn-base-plat` 命名空间，Service 为 `global-id-service`（8090/8091/8092）；应用业务 ID 已改由共享服务分配并拒绝 UUID；独立机器客户端已通过 base-plat 登记，项目 SDK 真实取号已通过；Pod 内完整写链路待验收 |
| M2M-03 | MCP 接口 | 未实现 | MCP transport、工具契约、输入/输出 schema、授权与错误边界；当前无 MCP 实现。复用已有授权 API 能力并纳入安全验收 |

当前保护上限和已有验证见 [容量验收说明](capacity-readiness.md)。复杂 Agent 编排、自动工作流设计器和自研数据库不纳入本轮必做范围。

## 当前能力证据

| 能力 | 当前证据 | 缺口 |
| --- | --- | --- |
| 检索与问答 API | `POST /api/v1/retrieval/search`、`POST /api/v1/rag/answer` | 按权限、容量与真实服务链路持续验收 |
| 文档替换 | `PUT /api/v1/documents/{document_id}`，管理员限定 | 继续验证授权、审计与失败恢复 |
| OIDC 机器 Bearer | 基础签名/issuer/audience/subject/过期校验和本地角色映射 | 严格 `typ`/`azp`/tenant/scope 尚缺，见 M2M-01 |
| MCP | 未发现实现 | 见 M2M-03 |
| 业务 ID authority | 共享 global-id-service 2/2 可用；调用方及数据库已改为严格十进制 ID | 模拟 authority 的故障／迁移验证通过；独立机器授权与真实 SDK 取号通过；Pod 内写入联调待完成，见 ID-01 |
| Kubernetes 共享依赖 | 已检查基础服务版本；knowledge namespace 与专属 PG18 数据库已建立，应用 Pod 尚未部署 | 兼容性和应用连接尚未验收，见 OPS-03 |

## 交付记录

- 2026-09-10：登记完整差距，优先启动 AUTH-01 至 AUTH-05；尚未将任何新功能标记完成。
- 2026-09-10：AUTH-01 至 AUTH-05 完成本地实现与 Astra 复审，Luna 执行；正式 IdP 联调和部署仍属于 OPS-02，不标记完成。
  - 修复审计资源 ID 长度、角色 ID 拼接超长、并发会话版本丢失、在途撤权遗漏、登录模式隔离、旧管理员兼容及公开占位签名密钥风险。
  - 补齐缺失的 `query_events` 迁移；真实 PostgreSQL 验证迁移、表/列一致性、账户生命周期与受控锁等待并发回归，Redis 验证登录限流路径。
  - 全套测试：149 通过、1 跳过；跳过 `test_pipeline_integration.py`，本轮未启动完整 Kafka/Qdrant/MinIO 入库测试依赖。Admin production build、JavaScript 语法、Compose 配置和 Kubernetes 渲染通过。
  - 浏览器实际通过初始化管理员登录、创建普通用户、独立会话登录、自助改密后重新登录；移动视口无横向溢出且提交按钮可滚动触达。截图命令因浏览器 daemon 资源错误未产出，不声称完成截图视觉验收。
  - HTTP OIDC 回调测试替换外部令牌交换，验证准入成功、未知/禁用拒绝、state/必要 Cookie 和模式切换；不替代真实 IdP 端到端验收。
  - 下一阶段优先 AUTH-07 全站写接口同源保护与 ACL-01 文档授权管理；其余任务保持原状态。
- 2026-09-10：AUTH-07 与 ACL-01 完成本地实现与复审。
  - 全部 POST/PUT/PATCH/DELETE 浏览器请求在统一中间件校验同源；机器 Bearer 调用不依赖浏览器 Cookie，认证仍由原接口完成。
  - 管理员可读取并替换本租户文档角色；空角色集合拒绝所有检索角色。授权变更审计、提升知识版本，并以“先收紧、后提交、再扩展”的顺序同步 Qdrant payload；最终同步失败时返回可重试错误且保持收紧状态。
  - 覆盖跨租户拒绝、角色撤销后内容拒绝、缓存版本提升、审计、同步失败及重试、浏览器同源拒绝和 Bearer API 边界。未执行真实 Qdrant/Kafka/MinIO 全链路验收，仍由 QA-02/OPS-01 跟踪。

- 2026-10-26：调整基础设施归属与环境接入要求，PostgreSQL 基线升为 18、Kafka 升为 4；localidc 复用 infra 中的 PostgreSQL 18.6／Kafka 4.3.1。
  - 已读取两个环境的 OIDC discovery，确认 localidc 与生产 issuer；没有执行真实用户登录或机器令牌联调。
  - 认证／权限／入库聚焦测试 48 项通过；事件与重试测试 3 项通过。隔离 PostgreSQL 18.6 的并发迁移测试 1 项通过，实例已清理。
  - aiokafka 0.14.0 对共享 Kafka 4.3.1 的 bootstrap、ApiVersions 与 metadata 读取成功；没有创建 topic 或发送消息，不等于生产／消费全链路验收。
  - Compose 配置、Kubernetes 渲染、Python 编译和 `uv lock --check` 通过。本机 Docker daemon 不可用，未启动完整 Compose；未迁移旧业务数据、部署应用或修改 infra。MCP、完整机器 claims/scope、global-id-service 接入仍未完成。

- 2026-10-26（部署与发号改造）：应用 namespace 按要求改为 `knowledge`；复用 infra PostgreSQL 18.6，创建专属数据库及最小权限登录，连接仅保存在 Kubernetes Secret 和忽略的私有文件中。
  - 创建前实查目标数据库不存在；新库迁移前 public 表数为 0。已执行前向 Alembic 迁移，未向业务表写入本地生成 ID。
  - 旧业务 ID 通过共享 authority 的批量接口分配后在事务内重映射；接口、ORM 和数据库约束拒绝 UUID，无运行时双格式兼容。旧外部 subject 与原件对象定位符保留其外部契约。
  - 当前 IdP 管理 API 与正式合同核对确认：新机器登记仅支持自身 audience（另有 configuration-service 特例），未支持独立客户端访问 global-id-service；企业 Provider 列表为空。未绕过控制平面、借用其他客户端或创建错误授权。
  - 现有模型配置指向不可达的本机模型端点，专属 S3 授权和域名准入也未完成；应用 Pod 尚未发布。现网未发现该项目旧库；本机通用 deploy 卷归属未证实，保持不动，不声称已完成旧业务数据搬迁。

  - 实际数据库已到 `a2b3c4d5e6f7`；读回确认业务列为19字符，外部租户列保留128字符，local_user/local_admin 约束已生效。新库无业务文档；没有伪造发号记录填充业务数据。
  - 隔离 PG18 的10项迁移／并发／schema 验证通过。全套本地回归156项通过、14项因外部依赖等条件跳过；剩余1项是旧管理员测试夹具，改为从模拟 authority 取得 ID 后单项复验通过。真实 pipeline 不使用模拟发号客户端，必须配置独立机器凭据。
  - 存储桶检查改为单桶 head，不要求应用列举全部共享桶；覆盖存在、缺失、403和服务故障。镜像上下文采用白名单，排除本地凭据、虚拟环境和临时合同源码。

  - 镜像 `sn-knowledge:localidc-1ba9b1c9c3fd6f63` 已按固定 Python digest 和 uv.lock 构建为 amd64；断网容器内模块导入、迁移 head 打包及缺少 authority 配置拒绝发号检查通过。镜像未导入 localidc 节点，应用 Deployment 未应用；部署候选清单已校验 namespace、镜像、数据库 Secret 和客户端标签。

- 2026-10-26（base-plat 正式接入）：复用平台已有的 scoped global-ID 登记及 local HUMAN bootstrap 流程，完成 `global-id-sn-knowledge`、`sn-knowledge` 两个专属客户端；这取代上轮只依据通用 IdP 管理合同得出的登记阻塞判断。
  - JWT 签名、issuer、audience、scope、azp 校验及项目 SDK 真实分配3个 snowflake-v2 ID 通过；knowledge 专用 Secret 精确回读，无其他调用方凭据复用或轮换。
  - SeaweedFS 使用已有 filer IAM 原生接口，无重启登记 knowledge 身份与 knowledge bucket。严格 TLS、自有桶 head、诊断对象 put/get/delete 和其他桶403通过；其他身份与静态配置摘要未变。
  - localidc 候选清单已绑定专属发号、OIDC、存储 Secret 及公共存储 CA。真人登录、Pod 内写入与完整 RAG 仍待发布；模型推理服务／Provider 在 localidc 缺失，未自动安装重型组件。
