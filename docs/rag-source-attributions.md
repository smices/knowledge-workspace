# RAG 能力与来源记录

> 本文档单独记录能力研究来源、版本和使用边界。主设计文档只描述 `sn_knowledge` 自身设计。
>
> 核验日期：2026-08-17

## 来源与版本

| 来源 | 核验版本 | 采用的能力范围 | 使用方式 |
|---|---|---|---|
| `infiniflow/ragflow` | `v0.26.4` | 文档解析、结构恢复、Chunk 生命周期、知识库处理状态 | 独立实现设计，不复制代码 |
| `langgenius/dify` | `1.16.1` | 租户/应用边界、RAG 服务分层、后台任务、模型配置 | 独立实现设计，不引入完整平台 |
| `labring/FastGPT` | `v4.16.0` | 知识库、Chunk 管理、混合检索、重排、引用、API | 独立实现核心业务模型 |
| `1Panel-dev/MaxKB` | `v2.10.5-lts` | 私有化部署、初始化、管理和企业集成体验 | 作为运维和产品体验参考 |
| `QuivrHQ/quivr` | `core-0.0.33` | 轻量 RAG Core、Pipeline 组合、可插拔模型思路 | 仅参考模块边界 |
| `qdrant/qdrant-rag-eval` | 无 latest release | Dense/Sparse/Hybrid、Rerank、RAGAS、检索评测 | 参考评测方法和实验结构 |
| Project Gutenberg | eBooks 23863、23950、23962、24264 | 四大古典名著公开测试语料 | 原文及其 Gutenberg License 随 `eval/corpus/` 保留 |

## 记录规则

- 不将外部项目作为 `sn_knowledge` 的运行时依赖。
- 不复制未经确认的源码片段、模型文件、图片或文档内容。
- 新增实现通过本项目的领域模型、错误处理、RBAC 和测试重新表达。
- 如未来确实复制代码，必须在本文件记录：文件路径、commit、许可证、版权声明、修改范围和发布义务。
- 版本只作为能力核验锚点；外部项目继续变化时重新评估，不自动跟随 latest。

## 官方来源

- https://github.com/infiniflow/ragflow
- https://github.com/infiniflow/ragflow/releases/tag/v0.26.4
- https://github.com/langgenius/dify
- https://github.com/langgenius/dify/releases/tag/1.16.1
- https://github.com/labring/FastGPT
- https://github.com/labring/FastGPT/releases/tag/v4.16.0
- https://github.com/1Panel-dev/MaxKB
- https://github.com/1Panel-dev/MaxKB/releases/tag/v2.10.5-lts
- https://github.com/QuivrHQ/quivr
- https://github.com/QuivrHQ/quivr/releases/tag/core-0.0.33
- https://github.com/qdrant/qdrant-rag-eval
- https://www.gutenberg.org/ebooks/23863
- https://www.gutenberg.org/ebooks/23950
- https://www.gutenberg.org/ebooks/23962
- https://www.gutenberg.org/ebooks/24264

## 本项目的独立创新点

- PostgreSQL 事实源与 Qdrant 可重建索引的明确分离。
- Kafka outbox 保障入库事件不丢失。
- 文档版本、Chunk 版本和权限快照同时可追踪。
- 权限先于召回，并在 Qdrant 查询边界内强制执行。
- 允许人工编辑 Chunk，但不被自动解析重跑静默覆盖。
- 将 permission leakage rate 作为发布门禁，而不只看答案质量。
- 使用结构感知中间文档格式统一 PDF、DOCX、表格和 OCR 输入。
