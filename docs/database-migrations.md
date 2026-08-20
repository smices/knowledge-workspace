# 数据库迁移

项目使用 Alembic 管理 PostgreSQL 结构。执行迁移前应备份数据库，并确认 `DATABASE_URL` 指向目标环境。

```bash
.venv/bin/alembic upgrade head
```

Docker 和 Kubernetes 部署会在 API 启动前执行同一迁移。回滚必须先审查对应 revision 的 `downgrade()`，不得在生产环境直接尝试未验证的降级。
