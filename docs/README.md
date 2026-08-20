# 项目文档

## 使用与运维

- [部署与运行](../deploy/README.md)：本机源码、Docker Compose、Kubernetes、品牌配置和上线检查。
- [OpenIdentity 集成](oidc-integration.md)：OIDC 登录、回跳、会话和生产配置。
- [数据库迁移](database-migrations.md)：Alembic 迁移命令与约束。

## RAG 设计与质量

- [系统设计](rag-system-design.md)：服务边界、数据流、权限和技术选型。
- [能力组合设计](rag-capability-plan.md)：解析、检索、生成、引用和任务治理。
- [评测说明](rag-evaluation.md)：数据集、质量门槛和效率指标。
- [研究来源记录](rag-source-attributions.md)：参考项目、版本和使用边界。

## 代码入口

- `app/`：FastAPI、认证、检索、问答、管理接口。
- `worker/`：文档解析、切分、向量化和任务消费。
- `web/`：主问答页。
- `admin/`：Umi Max / Ant Design Pro 管理端。
- `tests/`：核心接口、认证、检索和关系图谱测试。
- `eval/`：离线评测数据、执行器和结果。
- `deploy/`：运行脚本、环境模板和部署清单。
