"""Allocate installation administrator principals; retain the credential config key."""
from alembic import op
import sqlalchemy as sa

revision = "a2b3c4d5e6f7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None

TABLES = ("tenants", "principals", "roles", "permissions", "role_permissions", "principal_roles",
          "local_admin_credentials", "local_user_credentials", "knowledge_bases", "documents",
          "document_versions", "document_grants", "chunks", "ingestion_jobs", "event_outbox",
          "ingestion_dead_letters", "audit_events", "query_events", "answer_feedback",
          "knowledge_relations", "entity_aliases")
PRINCIPAL_FIELDS = {"principal_id", "created_by", "approved_by", "subject"}


def upgrade():
    from app.global_ids import new_ids, validate_id
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        raise RuntimeError("Administrator ID migration requires PostgreSQL transactional DDL")
    metadata = sa.MetaData()
    metadata.reflect(connection, only=TABLES)
    tables = metadata.tables
    connection.execute(sa.text("LOCK TABLE " + ", ".join('"' + name + '"' for name in sorted(tables)) + " IN ACCESS EXCLUSIVE MODE"))
    rows = {name: list(connection.execute(table.select()).mappings()) for name, table in tables.items()}
    old = sorted(row["id"] for row in rows["principals"] if row["principal_type"] == "local_admin")
    allocated = new_ids(len(old)) if old else []
    if len(allocated) != len(old):
        raise RuntimeError("Authority returned incomplete administrator IDs")
    allocated = [validate_id(value) for value in allocated]
    existing = {row["id"] for items in rows.values() for row in items if "id" in row}
    if len(set(allocated)) != len(allocated) or set(allocated) & existing:
        raise RuntimeError("Authority administrator ID allocation collided with existing IDs")
    mapping = dict(zip(old, allocated))

    def remap_json(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if (key in PRINCIPAL_FIELDS or (key == "resource_id" and value.get("resource_type") == "principal")) and isinstance(item, str):
                    result[key] = mapping.get(item, item)
                elif key == "principal_ids" and isinstance(item, list):
                    if not all(isinstance(entry, str) for entry in item):
                        raise RuntimeError("Ambiguous principal IDs in persisted JSON")
                    result[key] = [mapping.get(entry, entry) for entry in item]
                else:
                    result[key] = remap_json(item)
            return result
        if isinstance(value, list):
            return [remap_json(item) for item in value]
        return value

    foreign_keys = []
    if mapping:
        for name, table in tables.items():
            for fk in table.foreign_key_constraints:
                foreign_keys.append((name, fk.name, fk.deferrable, fk.initially))
                op.execute(sa.text(f'ALTER TABLE "{name}" ALTER CONSTRAINT "{fk.name}" DEFERRABLE INITIALLY DEFERRED'))
        op.execute(sa.text("SET CONSTRAINTS ALL DEFERRED"))
        for name, items in rows.items():
            table = tables[name]
            for row in items:
                updates = {}
                for column in table.c:
                    value = row[column.name]
                    if (column.name in PRINCIPAL_FIELDS or (name == "principals" and column.name == "id") or
                            (name == "audit_events" and column.name == "resource_id" and row["resource_type"] == "principal")):
                        updates[column.name] = mapping.get(value, value)
                    elif isinstance(column.type, sa.JSON):
                        updates[column.name] = remap_json(value)
                if updates:
                    condition = sa.and_(*(column == row[column.name] for column in table.primary_key))
                    connection.execute(table.update().where(condition).values(**updates))
        connection.execute(tables["tenants"].update().values(knowledge_revision=tables["tenants"].c.knowledge_revision + 1))
        op.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))
        for name, constraint, deferrable, initially in foreign_keys:
            mode = "DEFERRABLE INITIALLY " + (initially or "IMMEDIATE") if deferrable else "NOT DEFERRABLE"
            op.execute(sa.text(f'ALTER TABLE "{name}" ALTER CONSTRAINT "{constraint}" {mode}'))
    op.drop_constraint("ck_principals_local_user_business_id", "principals", type_="check")
    op.create_check_constraint("ck_principals_local_business_id", "principals",
        "principal_type NOT IN ('local_user', 'local_admin') OR (length(id) <= 19 AND CAST(id AS BIGINT) > 0 "
        "AND CAST(CAST(id AS BIGINT) AS VARCHAR) = id)")


def downgrade():
    raise RuntimeError("Administrator ID remapping cannot be reversed; restore the pre-migration backup")
