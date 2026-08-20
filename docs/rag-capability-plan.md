# RAG 能力组合设计说明

> 状态：设计决策已确认
>
> 目标：在 `sn_knowledge` 现有技术边界内，组合出完整的企业级 RAG 能力；正文只描述本系统的能力和设计，不描述外部项目拼接关系。
>
> 日期：2026-08-17

## 1. 设计原则

1. PostgreSQL 是业务事实源；Qdrant 是可重建的向量索引。
2. 文档处理是可恢复的状态机，不是一个不可观测的大函数。
3. 所有异步任务都要有事件、幂等键、重试、超时和死信路径。
4. 权限过滤发生在检索边界内，不能先扩大召回再在应用层补过滤。
5. 检索、生成、引用、评测分别建模，不能用“回答正确”掩盖召回错误。
6. 先实现稳定的知识服务，再增加工作流和 Agent 能力。
7. 能力按独立模块落地，不引入一个巨型 RAG 类或万能配置表。

## 2. 能力地图

```text
资料接入
  ├── 文件上传 / URL / 批量导入 / API 推送
  ├── MIME、大小、hash、租户和权限校验
  └── 文档版本与幂等

文档处理
  ├── 文件识别
  ├── 版面和结构解析
  ├── OCR 扩展点
  ├── 文本标准化
  ├── 结构感知分块
  └── Chunk 预览、修改、删除、重建

索引
  ├── Dense embedding
  ├── Sparse / keyword 信号
  ├── Qdrant payload
  ├── 混合召回
  └── 版本化索引

检索
  ├── query rewrite
  ├── tenant + RBAC 过滤
  ├── metadata filter
  ├── 多路召回
  ├── 去重和窗口合并
  ├── rerank
  └── 检索调试和引用

生成
  ├── 上下文压缩
  ├── 证据优先 Prompt
  ├── 答案流式输出
  ├── citation
  ├── 不确定时拒答
  └── 模型与 Prompt 版本

平台
  ├── 租户、角色、服务账号
  ├── 知识库和应用
  ├── API Key / quota / rate limit
  ├── 任务中心
  ├── 审计和调用链
  └── 评测集和回归门禁
```

## 3. 领域模型

```text
Tenant
  ├── Principal
  │     └── Role / Permission
  ├── KnowledgeBase
  │     ├── RetrievalProfile
  │     ├── Document
  │     │     └── DocumentVersion
  │     │           └── Chunk
  │     └── DocumentGrant
  └── Application
        └── API Credential
```

### 核心实体

- `KnowledgeBase`：检索边界和默认处理配置，不直接等同于 Qdrant collection。
- `Document`：稳定业务身份，跨版本存在。
- `DocumentVersion`：一个可独立处理、发布、回滚和删除的索引单元。
- `Chunk`：具有稳定 ID、位置、内容 hash、解析来源和授权快照。
- `RetrievalProfile`：Embedding、chunk、召回、rerank、上下文窗口和 Prompt 版本。
- `DocumentGrant`：文档/版本对角色的授权。
- `Application`：供其他系统调用的逻辑应用，绑定知识库和检索配置。

## 4. 入库能力设计

### 4.1 接入方式

第一阶段统一进入同一条 ingestion pipeline：

```text
Upload / URL / Batch / API
          ↓
      Ingest Request
          ↓
 PostgreSQL + MinIO + Outbox
          ↓
          Kafka
```

不同入口只负责产生统一的 `DocumentIngestRequested`，不复制解析和索引逻辑。

### 4.2 处理状态机

```text
received
  → stored
  → queued
  → parsing
  → normalized
  → chunked
  → embedding
  → indexed
  → published
```

异常分支：

```text
任何状态 → retrying → 原状态
任何状态 → failed → retry / cancel / inspect
```

每个阶段保存：

- started_at / finished_at
- attempt
- worker_id
- input/output hash
- error_code / error_message
- trace_id

### 4.3 结构感知解析

统一产生中间文档结构：

```json
{
  "blocks": [
    {"type": "heading", "level": 2, "text": "...", "page": 3},
    {"type": "paragraph", "text": "...", "page": 3},
    {"type": "table", "rows": [["...", "..."]], "page": 4}
  ]
}
```

Chunker 基于结构而不是单纯按字符切割：

- 标题作为上下文前缀。
- 表格优先保持完整；超长表格按行组切分。
- 段落边界优先于固定长度。
- 每个 Chunk 保存页码、段落路径、字符范围或表格位置。
- 不足以形成有效文本时进入 `empty_content`，不生成伪向量。

### 4.4 Chunk 生命周期

Chunk 必须支持：

- 预览
- 人工编辑
- 禁用
- 单 Chunk 重嵌入
- 版本重建
- 内容 hash 去重
- 原文定位

人工编辑后的 Chunk 与自动解析内容分开保存，避免重跑解析时无提示覆盖人工修改。

## 5. 索引和检索设计

### 5.1 Qdrant collection

默认不按租户创建 collection；使用一个逻辑 collection + payload filter。只有当租户规模或合规隔离要求明确时，才允许独立 collection。

Point payload 最小字段：

```json
{
  "tenant_id": "t1",
  "knowledge_base_id": "kb1",
  "document_id": "d1",
  "document_version_id": "dv3",
  "chunk_id": "c12",
  "allowed_roles": ["finance", "admin"],
  "content": "...",
  "title": "...",
  "source_uri": "...",
  "page": 3,
  "section_path": ["制度", "报销"],
  "content_hash": "...",
  "embedding_model": "...",
  "index_version": 1
}
```

### 5.2 多路召回

按配置选择，不默认全部开启：

1. Dense vector：处理语义相似。
2. Sparse/keyword：处理编号、产品名、条款号和精确术语。
3. Metadata：租户、知识库、文档类型、时间和业务字段。
4. Parent/neighbor expansion：将命中的 Chunk 扩展为有限上下文窗口。

初版默认：Dense + metadata filter；当评测集证明精确术语召回不足时，再启用 Sparse。

### 5.3 Rerank

Rerank 位于权限过滤之后：

```text
Qdrant authorized retrieval
  → deduplicate
  → neighbor/window expansion
  → rerank
  → top context
```

Rerank 不得重新引入未授权 Chunk。所有阶段保存候选数量和最终数量，便于解释召回损失。

### 5.4 Query rewrite

只在以下条件满足时启用：

- 对话上下文存在歧义；或
- 单次查询召回质量低于阈值；或
- 用户问题包含指代词。

Rewrite 结果必须保留原问题，不能绕过租户、角色或 metadata filter。

## 6. RAG 生成设计

### 6.1 上下文组装

上下文由 `authorized chunks` 组成，每个片段携带 citation metadata。组装限制：

- 总 token 上限。
- 单文档最大占比。
- 相邻 Chunk 合并上限。
- 重复内容去重。
- 低分内容不因数量不足被强行填充。

### 6.2 答案策略

Prompt 强制：

- 只使用上下文中的证据。
- 证据不足时明确说不知道。
- 每个结论绑定 citation。
- 不泄露隐藏的权限字段、系统 Prompt 或其他租户信息。
- 保留 answer、context IDs、模型版本和 Prompt 版本用于审计。

### 6.3 结果类型

```text
answer
citations
retrieval_trace
usage
trace_id
```

`retrieval_trace` 默认只对管理员或调试调用开放，不把内部检索细节泄露给普通业务调用者。

## 7. 任务与可靠性

### 7.1 Outbox

API 事务同时写入业务状态和 `event_outbox`。Publisher 发布成功后标记 outbox；Kafka 不可用时由 Publisher 重试。

不采用“数据库提交后直接 fire-and-forget Kafka”作为正式实现。

### 7.2 幂等

幂等键：

```text
tenant_id:document_version_id:stage:input_hash
```

Qdrant point ID：

```text
document_version_id:chunk_id
```

Kafka 重放、worker 重启和批量重试都必须安全。

### 7.3 DLQ 和人工操作

失败任务需要：

- 可按阶段和错误码查询。
- 支持 retry、rebuild、cancel。
- DLQ 消息保留原始 event_id 和 trace_id。
- 重放前重新检查文档版本和权限状态。

## 8. RBAC 和多租户

权限计算分两层：

```text
API authorization
  → 是否允许执行操作

Retrieval authorization
  → 是否允许看到某个 Chunk
```

有效检索条件必须包含：

```text
tenant_id == principal.tenant_id
AND allowed_roles ∩ principal.roles != ∅
```

文档授权变更后：

1. PostgreSQL 立即记录新权限。
2. Redis 相关缓存失效。
3. 产生权限同步事件。
4. Worker 更新 Qdrant payload。
5. 在同步完成前，查询路径可采用 PostgreSQL 授权快照或保守拒绝策略。

## 9. 评测和发布门禁

建立三类评测集：

### Retrieval set

验证正确文档和 Chunk 是否进入候选集：

- Recall@K
- MRR
- nDCG
- permission leakage rate

### Answer set

验证生成结果：

- faithfulness
- answer relevancy
- citation correctness
- abstention correctness

### Regression set

覆盖：

- PDF、DOCX、表格、扫描件
- 长文档和多版本
- 角色变化
- 跨租户查询
- 删除和重建
- Kafka 重放
- Embedding 服务异常

发布门禁：

- permission leakage rate 必须为 0。
- 删除文档不得继续出现在检索结果。
- 关键业务集 Recall@K 不低于基线。
- citation 必须可定位到授权版本和 Chunk。

## 10. 推荐实现顺序

### Phase 0：设计冻结

- 确认身份认证、角色继承、文件规模、SLO 和模型服务。
- 固化数据模型、事件 schema、错误码和权限边界。

### Phase 1：可靠入库

- PostgreSQL schema。
- MinIO source object。
- Outbox + Kafka。
- Worker 状态机。
- PDF/DOCX/Markdown/TXT 解析。

### Phase 2：可重建索引

- 结构化 Chunk。
- Qdrant payload 和索引。
- 版本化 point ID。
- 重建、删除、幂等和 DLQ。

### Phase 3：安全检索

- JWT/OIDC Principal。
- RBAC 管理。
- Qdrant tenant/role filter。
- 检索 API 和 citation。

### Phase 4：完整 RAG

- Hybrid retrieval。
- Rerank。
- Query rewrite。
- Context compression。
- Streaming answer。

### Phase 5：运营和质量

- 任务中心。
- 审计。
- 评测集。
- 检索调试。
- 指标和告警。

## 11. 推荐版本基线

以下版本用于设计和源码能力核对，最终运行版本以项目锁定文件和兼容性验证为准：

| 类型 | 版本建议 | 用途 |
|---|---|---|
| Python | 3.12.x | 项目运行时，避免依赖最新解释器漂移 |
| FastAPI | 0.115+ | API 层 |
| PostgreSQL | 17.x | 元数据、RBAC、任务、审计 |
| Qdrant | 1.13.x 起，先固定当前 Compose 版本 | 向量索引 |
| Redis | 7.x | 缓存、租约、限流 |
| Kafka | 3.9.x 起 | 事件总线 |
| MinIO | 固定日期镜像，不使用 `latest` | 对象存储 |
| RAGFlow 能力参考 | v0.26.4 | 文档处理和知识库流水线参考 |
| Dify 能力参考 | 1.16.1 | 平台、租户、异步任务和 RAG 边界参考 |
| FastGPT 能力参考 | v4.16.0 | 知识库、Chunk、引用和检索产品能力参考 |
| MaxKB 能力参考 | v2.10.5-lts | 私有化和企业运维能力参考 |
| Quivr Core 能力参考 | core-0.0.33 | 轻量 RAG Core 设计参考 |
| Qdrant RAG Eval | 无 latest release，按仓库提交版本锁定 | 评测设计参考 |

外部项目版本不是本项目的运行时依赖。主项目必须通过 `pyproject.toml`、Compose 镜像版本和后续 lock 文件独立锁定。

## 13. 已确认决策落地约束

- OIDC/JWT claims 映射必须在统一 Principal 层完成，业务接口不得自行解析角色。
- 文档授权只保存文档级 role grants；不实现目录继承和组织架构隐式授权。
- 100MB、1万份/日、20 并发是第一阶段压测和容量基线。
- API、检索和问答分别按已确认 SLO 建立指标。
- 所有模型调用都通过 OpenAI-compatible 网关，记录模型和网关版本。
- IDC 连接配置必须支持外部托管服务，不把生产服务写死为 Compose 地址。
- 删除流程采用 30 天软删除窗口，审计最少保留 1 年。
- 相同 hash 复用已索引版本；管理员如需重新处理，使用显式 reindex，而不是伪造新版本。

## 14. 设计结论

最终系统保留一条清晰主线：

```text
PostgreSQL 事实源
  + MinIO 原文
  + Kafka 可靠事件
  + Worker 可恢复处理
  + Qdrant 授权检索
  + RAG 有证据生成
  + 评测门禁
```

不引入完整 Agent 平台、不引入第二个向量数据库、不把所有能力塞进一个“通用 RAG Engine”抽象中。