"""Replace unpublished local IDs using the shared authority, without deleting data."""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "9a4e5f6b7c81"
branch_labels = None
depends_on = None

TABLES = (
    "tenants", "principals", "roles", "permissions", "role_permissions", "principal_roles",
    "local_admin_credentials", "local_user_credentials", "knowledge_bases", "documents",
    "document_versions", "document_grants", "chunks", "ingestion_jobs", "event_outbox",
    "ingestion_dead_letters", "audit_events", "query_events", "answer_feedback",
    "knowledge_relations", "entity_aliases",
)

# Explicit semantic targets also cover references that intentionally have no FK.
TARGETS = {
    "role_id": "roles", "permission_id": "permissions", "knowledge_base_id": "knowledge_bases",
    "document_id": "documents", "document_version_id": "document_versions", "chunk_id": "chunks",
    "event_id": "event_outbox", "answer_id": "answers", "job_id": "ingestion_jobs",
    "principal_id": "principals", "created_by": "principals", "approved_by": "principals",
    "subject": "principals",
}
RESOURCES = {
    "role": "roles", "permission": "permissions", "knowledge_base": "knowledge_bases",
    "document": "documents", "document_version": "document_versions", "chunk": "chunks",
    "event": "event_outbox", "answer": "answers", "ingestion_job": "ingestion_jobs",
    "principal": "principals", "entity_alias": "entity_aliases", "knowledge_relation": "knowledge_relations",
}


def upgrade():
    from app.global_ids import new_ids, validate_id

    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        raise RuntimeError("Business-ID data migration requires PostgreSQL transactional DDL")
    metadata = sa.MetaData()
    metadata.reflect(connection, only=TABLES)
    tables = dict(metadata.tables)
    # Exclude writes for the complete read/allocate/remap transaction.
    connection.execute(sa.text("LOCK TABLE " + ", ".join('"' + n + '"' for n in sorted(tables)) + " IN ACCESS EXCLUSIVE MODE"))
    rows = {name: list(connection.execute(table.select()).mappings()) for name, table in tables.items()}
    business = {name for name, table in tables.items() if "id" in table.c and
                (getattr(table.c.id.type, "length", None) == 36 or name in {"roles", "permissions"})}
    columns = {}
    for name, table in tables.items():
        columns[name] = {}
        for column in table.c:
            target = name if column.name == "id" and (name in business or name == "principals") else TARGETS.get(column.name)
            if column.name == "aggregate_id":
                target = "documents"  # all current outbox events aggregate documents
            if target:
                columns[name][column.name] = target

    mapping = {name: {} for name in business | set(TARGETS.values())}
    # External identities and the fixed installation administrator stay opaque.
    for row in rows.get("principals", []):
        if row["principal_type"] == "local_user":
            mapping["principals"][row["id"]] = None

    def remember(target, value):
        if value is not None and target != "principals":
            mapping[target].setdefault(value, None)

    def json_values(value, collect=False):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                target = TARGETS.get(key) or (TARGETS.get(key[:-1]) if key.endswith("_ids") else None)
                if target and isinstance(item, (str, list)):
                    values = item if isinstance(item, list) else [item]
                    if not all(isinstance(v, str) for v in values):
                        raise RuntimeError("Ambiguous non-string business ID in persisted JSON")
                    if collect:
                        for v in values:
                            remember(target, v)
                    result[key] = [mapping[target].get(v, v) for v in values] if isinstance(item, list) else mapping[target].get(item, item)
                else:
                    result[key] = json_values(item, collect)
            return result
        if isinstance(value, list):
            return [json_values(item, collect) for item in value]
        return value

    for name, items in rows.items():
        for row in items:
            for column, target in columns[name].items():
                remember(target, row[column])
            if name == "audit_events" and row["resource_id"] is not None:
                target = RESOURCES.get(row["resource_type"])
                if target is None:
                    raise RuntimeError("Unrecognized audit resource type; explicit ID mapping required")
                remember(target, row["resource_id"])
            for column in tables[name].c:
                if isinstance(column.type, sa.JSON):
                    json_values(row[column.name], collect=True)

    keys = [(target, old) for target in sorted(mapping) for old in sorted(mapping[target])]
    allocated = new_ids(len(keys)) if keys else []
    if len(allocated) != len(keys):
        raise RuntimeError("Authority returned an incomplete ID allocation")
    allocated = [validate_id(value) for value in allocated]
    old_values = {value for target in mapping.values() for value in target}
    old_values.update(row["id"] for items in rows.values() for row in items if "id" in row)
    if len(set(allocated)) != len(allocated) or set(allocated) & old_values:
        raise RuntimeError("Authority allocation collided with existing IDs")
    for (target, old), new in zip(keys, allocated):
        mapping[target][old] = new

    # PostgreSQL checks deferred FKs at commit; restore their original properties only
    # after validating the entire remapped graph.
    foreign_keys = []
    for name, table in tables.items():
        for fk in table.foreign_key_constraints:
            foreign_keys.append((name, fk.name, fk.deferrable, fk.initially))
            op.execute(sa.text(f'ALTER TABLE "{name}" ALTER CONSTRAINT "{fk.name}" DEFERRABLE INITIALLY DEFERRED'))
    op.execute(sa.text("SET CONSTRAINTS ALL DEFERRED"))
    for name, items in rows.items():
        table = tables[name]
        for row in items:
            updates = {column: mapping[target].get(row[column], row[column]) for column, target in columns[name].items()}
            if name == "audit_events" and row["resource_id"] is not None:
                updates["resource_id"] = mapping[RESOURCES[row["resource_type"]]].get(row["resource_id"], row["resource_id"])
            for column in table.c:
                if isinstance(column.type, sa.JSON):
                    updates[column.name] = json_values(row[column.name])
            if updates:
                condition = sa.and_(*(column == row[column.name] for column in table.primary_key))
                connection.execute(table.update().where(condition).values(**updates))
    connection.execute(tables["tenants"].update().values(knowledge_revision=tables["tenants"].c.knowledge_revision + 1))
    op.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))
    for name, constraint, deferrable, initially in foreign_keys:
        mode = "DEFERRABLE INITIALLY " + (initially or "IMMEDIATE") if deferrable else "NOT DEFERRABLE"
        op.execute(sa.text(f'ALTER TABLE "{name}" ALTER CONSTRAINT "{constraint}" {mode}'))
    for name, table in tables.items():
        for column in table.c:
            if column.name != "tenant_id" and (getattr(column.type, "length", None) == 36 or
                    (column.name == "id" and name in {"roles", "permissions"}) or
                    any(fk.column.table.name in business for fk in column.foreign_keys)):
                op.alter_column(name, column.name, existing_type=column.type, type_=sa.String(19), existing_nullable=column.nullable)
                op.create_check_constraint(f"ck_{name}_{column.name}_business_id", name,
                    f"length({column.name}) <= 19 AND CAST({column.name} AS BIGINT) > 0 "
                    f"AND CAST(CAST({column.name} AS BIGINT) AS VARCHAR) = {column.name}")

    op.alter_column("entity_aliases", "tenant_id", existing_type=sa.String(36),
                    type_=sa.String(128), existing_nullable=False)
    op.create_check_constraint("ck_principals_local_user_business_id", "principals",
        "principal_type != 'local_user' OR (length(id) <= 19 AND CAST(id AS BIGINT) > 0 "
        "AND CAST(CAST(id AS BIGINT) AS VARCHAR) = id)")


def downgrade():
    raise RuntimeError("Business ID remapping cannot be reversed; restore the pre-migration backup")
