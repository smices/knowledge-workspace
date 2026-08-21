# 企业知识库 RAG 系统设计

> 状态：设计决策已确认
>
> 项目：`sn_knowledge`
>
> 目标：将企业资料实时、安全地压入知识库，并向其他系统提供低延迟、可引用、按租户和角色隔离的 RAG 检索与问答能力。
>
> 设计附件：`docs/rag-capability-plan.md`（能力组合与版本基线）、`docs/rag-source-attributions.md`（来源记录）。

## 1. 设计决策

| 领域 | 决策 |
|---|---|
| 向量数据库 | Qdrant |
| 业务事实源 | PostgreSQL |
| 原始文件 | MinIO（IDC 可替换为兼容 S3 的对象存储） |
| 事件总线 | Kafka |
| 缓存与协调 | Redis，不保存唯一事实 |
| API | FastAPI，REST/JSON |
| 本地运行 | OrbStack + Docker Compose |
| Python 环境 | 项目根目录 `.venv` |
| 权限 | 租户隔离 + RBAC + 文档角色授权 |
| 远程 Git | 暂不配置，由用户后续合入线上 Git |

Qdrant 只负责向量索引和检索 payload，不作为文档、权限、任务状态或审计的事实源。Qdrant 删除后必须能够从 PostgreSQL 和 MinIO 重建。

## 2. 目标与非目标

### 目标

- 支持 PDF、DOCX、Markdown、TXT，后续可扩展解析器。
- 上传后快速返回任务受理结果，解析/分块/Embedding/索引异步完成。
- 文档版本可追踪，重复事件和重试不会产生重复向量。
- 检索在 Qdrant 内部完成 tenant + role 权限过滤。
- 生成答案必须带文档、版本、chunk 和来源引用。
- 支持失败重试、死信、状态查询、删除和重建索引。
- 保留处理、授权和检索审计信息。
- API 与 worker 可独立扩容。

### 非目标

- 第一阶段不自研向量数据库、Embedding 模型或全文搜索引擎。
- 第一阶段不实现复杂知识图谱、Agent 编排和自动工作流设计器。
- 第一阶段不将 Redis 作为持久化任务队列或权限事实源。
- 第一阶段不把原始企业文件放入 Qdrant。

## 3. 总体架构

```text
                    ┌──────────────┐
                    │ 外部业务系统  │
                    └──────┬───────┘
                           │ REST/API
                    ┌──────▼───────┐
                    │ FastAPI API  │
                    │ Auth/RBAC    │
                    └──┬────┬───┬───┘
                       │    │   │
          metadata/RBAC│    │   │cache/lock
                       │    │   └──────► Redis
                       │    │
                       │    └──────────► MinIO 原文件
                       │
                       ▼
                  PostgreSQL
              文档/版本/权限/任务/审计
                       │
                 outbox/event
                       ▼
                     Kafka
                       │
                       ▼
              Ingestion Worker 集群
       parse → normalize → chunk → embed → upsert
                       │              │
                       │              └──────► Qdrant
                       └──────────────────────► PostgreSQL 状态

查询路径：
API → 身份认证 → PostgreSQL 获取有效角色/权限
→ Dense + sparse embedding → Qdrant RRF(tenant + RBAC filter)
    → evidence co-occurrence gate → LLM → answer + citations
```

认证身份来自 IdP；应用角色、应用内启用状态和审计记录以 PostgreSQL 为事实源。首次 OIDC 安装创建唯一的本地初始化管理员，凭安装 Secret 登录，仅可配置 IdP 账号的应用权限，且不承载 IdP 资料或密码。

## 4. 服务职责

### API

- 认证调用者并构建不可伪造的 Principal：`subject`、`tenant_id`、有效角色和权限。
- 校验文件类型、大小、租户边界和操作权限。
- 创建文档版本记录。
- 将原文件写入 MinIO。
- 使用 outbox 或等价可靠发布机制产生 Kafka 事件。
- 提供文档状态、检索、问答、删除、重建和管理员接口。

### PostgreSQL

建议核心表：

- `tenants`
- `principals`
- `roles`
- `permissions`
- `principal_roles`
- `documents`
- `document_versions`
- `document_role_grants`
- `ingestion_jobs`
- `event_outbox`
- `audit_events`

关键约束：

- `documents(tenant_id, id)` 唯一边界。
- `document_versions(document_id, version)` 唯一。
- `ingestion_jobs(document_version_id, job_type)` 唯一，支持幂等。
- 文档版本只有一个当前版本。
- 权限变更可审计，并触发 Qdrant payload 同步任务。

### MinIO

对象键建议：

```text
{tenant_id}/documents/{document_id}/versions/{version}/source/{filename}
```

数据库保存 object key、大小、MIME、hash、版本和上传者。删除文档时先标记数据库状态，再异步删除 Qdrant points 和对象；失败可重试。

### Kafka

建议 topic：

- `knowledge.document.accepted.v1`
- `knowledge.document.index.v1`
- `knowledge.document.delete.v1`
- `knowledge.document.rebuild.v1`
- `knowledge.document.dlq.v1`

消息必须包含：

```json
{
  "event_id": "uuid",
  "tenant_id": "tenant-a",
  "document_id": "uuid",
  "document_version_id": "uuid",
  "version": 3,
  "event_type": "document.index",
  "occurred_at": "ISO-8601",
  "trace_id": "uuid"
}
```

Kafka key 使用 `tenant_id:document_id`，保证同一文档事件有序。Consumer 使用手动提交 offset；业务成功后提交，失败按重试策略进入 retry/DLQ。

### Redis

只用于：

- L0 合并同一进程内并发的完全相同问答；最后一个等待者取消时，中断底层检索与生成。
- L1 复用规范化后完全相同的问题，缓存键包含租户、角色、知识版本、模型、Prompt 和生成参数。
- L2 仅在问题实体集合、检索证据集合一致且当前嵌入模型的向量相似度不低于 0.70 时复用；只缓存状态为“已回答”且每条结论均被证据直接支持的答案。
- 文档成功完成索引或删除时提升租户知识版本；旧版本缓存不再命中，无需全量扫描删除。
- 幂等短锁和租约。
- 限流和热点查询缓存。
- 任务进度的短期加速读取，最终状态仍在 PostgreSQL。

## 5. RAG 数据链路

### 入库

1. API 验证 Principal 和上传权限。
2. 创建 `document` 与 `document_version`，计算源文件 hash。
3. 同一租户下相同 hash 可直接复用或明确创建新版本，策略待确认。
4. 原文件写入 MinIO。
5. 写入 outbox 事件并提交 PostgreSQL 事务。
6. outbox publisher 发布 Kafka。
7. worker 领取任务并更新状态：`queued → processing → indexed`。
8. 解析文件并标准化文本。
9. 按文档类型和结构分块，记录 chunk index 和内容 hash。
10. 批量生成 Embedding。
11. 使用稳定 point ID 写入 Qdrant。
12. 成功后更新索引统计、版本状态和审计记录。

### Qdrant point payload

```json
{
  "tenant_id": "tenant-a",
  "document_id": "uuid",
  "document_version_id": "uuid",
  "document_version": 3,
  "chunk_id": "uuid",
  "chunk_index": 12,
  "content": "检索文本",
  "source_uri": "s3://...",
  "title": "制度文件",
  "allowed_roles": ["finance", "admin"],
  "content_hash": "sha256",
  "indexed_at": "ISO-8601"
}
```

Point ID 使用 `document_version_id:chunk_id`，禁止只使用 `document_id:chunk_index`，避免版本混写。

### 检索

1. 验证调用者身份。
2. 从可信来源取得有效租户和角色。
3. 生成 query embedding。
4. Qdrant 查询必须同时包含：
   - `tenant_id == principal.tenant_id`
- `allowed_roles` 与有效角色集合有交集
5. Dense 与 sparse 结果在 Qdrant 内使用 RRF 融合；实体共现门槛仅缩小已授权候选，不能替代权限过滤。
6. 构造上下文并生成答案。
7. 返回答案、引用、检索分数、模型版本和 trace ID。

### 问答响应

```json
{
  "answer_id": "uuid",
  "answer": "...",
  "answer_state": "answered",
  "liked": false,
  "feedback_token": "user-scoped-signature",
  "evidence_contract": [
    {"claim": "...", "evidence": [1], "support": "supported", "confidence": 0.92}
  ],
  "citations": [
    {
      "document_id": "uuid",
      "document_version": 3,
      "chunk_index": 12,
      "title": "制度文件",
      "source_uri": "s3://...",
      "score": 0.86
    }
  ],
  "cache": {"level": "generated|l0|l1|l2", "hit": false, "knowledge_revision": 12},
  "trace_id": "uuid"
}
```

## 6. RBAC 模型

- `tenant`：最高隔离边界。
- `principal`：用户或服务账号。
- `role`：租户内角色，例如 `finance`、`hr`、`admin`。
- `permission`：操作权限，例如 `document:upload`、`document:delete`、`knowledge:query`。
- `document_role_grant`：文档版本允许访问的角色集合。

安全规则：

- 不信任请求体中的 `tenant_id`、subject、effective roles。
- 普通上传者不能自行提升文档角色授权，必须拥有明确管理权限。
- 查询、问答、状态、删除、重建都要走统一授权函数。
- 跨租户 document ID、source URI、缓存键和 citation 均拒绝。
- 角色撤销必须有缓存失效或明确的最大生效延迟。

## 7. API 初版

```text
POST   /api/v1/documents
GET    /api/v1/documents/{document_id}
GET    /api/v1/documents/{document_id}/status
POST   /api/v1/documents/{document_id}/reindex
DELETE /api/v1/documents/{document_id}
POST   /api/v1/retrieval/search
POST   /api/v1/rag/answer
POST   /api/v1/admin/roles
POST   /api/v1/admin/document-grants
GET    /health/live
GET    /health/ready
```

接口应使用版本前缀、trace ID、统一错误格式和幂等键。上传返回 `202 Accepted` 与 `document_version_id`，不等待向量化完成。

## 8. 失败、重试和一致性

- PostgreSQL 写入成功、Kafka 发布失败：依赖 outbox 重试，不能丢任务。
- MinIO 写入成功、数据库失败：清理孤儿对象或后台 reconciliation。
- Kafka 重复消息：按 `document_version_id` 和 job 唯一键幂等。
- Embedding 部分失败：整个版本保持 processing/failed，不标记 indexed。
- Qdrant upsert 部分失败：重试同一批；point ID 稳定。
- 删除失败：数据库状态先变为 deleting，后台持续重试。
- worker 崩溃：任务租约过期后可重新领取。
- DLQ 消息必须可以人工查看、重放或终止。

## 9. 本地与 IDC

### 本地

- OrbStack 运行 Docker Compose。
- PostgreSQL、Qdrant、Kafka、Redis、MinIO 使用 Compose 服务和命名卷。
- Python 依赖安装到项目内 `.venv`。
- `.env` 仅本地存在，不提交。

### IDC

- PostgreSQL 使用已有 IDC 实例。
- Qdrant、Kafka、Redis、MinIO 的部署方式需根据 IDC 运维标准确认。
- 所有连接信息通过环境变量或 Secret 注入。
- 生产不使用默认密码；必须配置 TLS、备份、监控和访问控制。

## 10. 可观测性与验收

必须具备：

- API/worker trace ID。
- 文档版本处理耗时和每阶段耗时。
- Kafka lag、重试次数、DLQ 数量。
- Qdrant 查询延迟、命中数和过滤后结果数。
- Embedding/LLM token、耗时和失败率。
- RBAC 拒绝计数，不记录不必要的正文。
- 关系图谱仅持久化含 document version、chunk、原文片段的直接证据边；文档替换、重建或删除时必须失效对应边。

验收门槛：

1. 同一事件重放不会重复 chunk。
2. 跨租户查询返回零结果。
3. 不同角色只能看到授权文档。
4. 删除后无法继续检索对应版本。
5. Qdrant 清空后可由 PostgreSQL + MinIO 重建。
6. Kafka、worker、Qdrant、MinIO 任一短暂不可用时不会静默丢资料。
7. 返回答案的每个关键依据都能定位到 citation。

## 11. 已确认的项目决策

| 决策项 | 已确认方案 |
|---|---|
| 身份认证 | 生产使用 OpenIdentity OIDC Authorization Code + PKCE；服务端校验 issuer、audience、签名、nonce 和 claims 后建立短期 HttpOnly 会话，业务角色仍由本地 PostgreSQL 维护；JWT 仅保留给本地 API 开发模式 |
| 文档权限 | 文档级 RBAC；第一阶段不引入目录或组织架构继承 |
| 规模基线 | 单文件 ≤100MB；日增 ≤1万份；并发上传 ≤20 |
| 性能目标 | 上传受理 ≤1秒；检索 P95 ≤500ms；问答首 token ≤2秒 |
| 检索策略 | Qdrant dense + hashed lexical sparse vector，经 RRF 融合；collection 必须包含 `lexical` sparse vector |
| 模型接入 | 统一使用 OpenAI-compatible 网关，网关负责云模型和 IDC 模型路由 |
| IDC 运维 | 本项目提供本地 Compose；IDC Kafka、Redis、MinIO、Qdrant 由平台团队托管 |
| 保留策略 | 软删除 30 天；审计保留 1 年；原文件按业务规则保留 |
| 相同 hash | 同租户相同 hash 复用已有版本，不重复向量化 |

## 12. 后续变更规则

无。后续只有在规模、SLO、身份系统或合规策略变化时更新本设计。

在设计确认后，进入 Phase 1：可靠入库与 PostgreSQL Schema 设计。
