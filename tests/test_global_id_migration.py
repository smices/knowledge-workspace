"""Forward-only remapping is exercised against an isolated PostgreSQL database."""
from datetime import datetime
import importlib.util
import os
from pathlib import Path
import secrets

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("authority_migration", ROOT / "migrations/versions/a1b2c3d4e5f6_authority_business_ids.py")
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


@pytest.fixture
def database():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires isolated TEST_DATABASE_URL")
    engine = sa.create_engine(url)
    assert engine.dialect.name == "postgresql"
    schema = "global_id_test_" + secrets.token_hex(8)
    with engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    connection = engine.connect()
    connection.execute(sa.text(f'SET search_path TO "{schema}"'))
    connection.commit()
    try:
        with connection.begin(), Operations.context(MigrationContext.configure(connection)):
            script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
            revisions = list(script.walk_revisions(base="base", head=MIGRATION.down_revision))
            for revision in reversed(revisions):
                revision.module.upgrade()
        yield connection
    finally:
        connection.rollback()
        connection.close()
        with engine.begin() as cleanup:
            cleanup.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def upgrade(connection):
    with connection.begin(), Operations.context(MigrationContext.configure(connection)):
        MIGRATION.upgrade()


def populate(connection):
    metadata = sa.MetaData()
    metadata.reflect(connection)
    ids = {name: "legacy-" + name for name in metadata.tables if name != "alembic_version"}
    ids["tenants"] = "opaque-tenant"
    ids["principals"] = "local-user:legacy"
    ids["documents"] = "99"  # legacy decimal format alone is not proof of authority issuance
    for table in metadata.sorted_tables:
        row = {}
        for column in table.c:
            if column.name == "id":
                row[column.name] = ids[table.name]
            elif column.foreign_keys:
                row[column.name] = ids[next(iter(column.foreign_keys)).column.table.name]
            elif not column.nullable:
                if isinstance(column.type, sa.Integer):
                    row[column.name] = 1
                elif isinstance(column.type, sa.DateTime):
                    row[column.name] = datetime(2026, 1, 1)
                elif isinstance(column.type, sa.JSON):
                    row[column.name] = {}
                else:
                    row[column.name] = "fixture"
        if table.name == "principals":
            row["principal_type"] = "local_user"
        if table.name == "local_admin_credentials":
            continue  # installation admin is tested separately below
        if table.name in {"audit_events", "query_events", "answer_feedback"}:
            row["subject"] = ids["principals"]
        if table.name == "audit_events":
            row.update(resource_type="document", resource_id=ids["documents"],
                       details={"role_ids": [ids["roles"]], "document_id": ids["documents"], "text": ids["documents"]})
        if table.name == "answer_feedback":
            row["answer_id"] = "legacy-orphan-answer"
        if table.name == "event_outbox":
            row.update(aggregate_type="document", aggregate_id=ids["documents"],
                       payload={"event_id": ids[table.name], "document_id": ids["documents"],
                                "document_version_id": ids["document_versions"], "tenant_id": ids["tenants"]})
        if table.name == "ingestion_dead_letters":
            row.update(event_id="legacy-orphan-event", document_id=ids["documents"],
                       document_version_id=ids["document_versions"], payload={"event_id": "legacy-orphan-event"})
        connection.execute(table.insert().values(**row))
    connection.execute(sa.text("INSERT INTO principals (id, tenant_id, principal_type, status, session_version, created_at) VALUES ('idp:opaque', :tenant, 'idp', 'active', 0, now()), ('local:initial-admin', :tenant, 'local_admin', 'active', 0, now())"), {"tenant": ids["tenants"]})
    connection.commit()
    return ids


def test_populated_graph_remaps_all_references_and_enforces_constraints(database, monkeypatch):
    from app import global_ids
    ids = populate(database)
    monkeypatch.setattr(global_ids, "new_ids", lambda count: [str(10000 + i) for i in range(count)])
    original_foreign_keys = sa.inspect(database).get_foreign_keys("documents")
    database.rollback()
    upgrade(database)
    assert sa.inspect(database).get_foreign_keys("documents") == original_foreign_keys
    metadata = sa.MetaData()
    metadata.reflect(database)
    rows = {name: database.execute(table.select()).mappings().all() for name, table in metadata.tables.items()}
    doc = rows["documents"][0]["id"]
    local = next(row["id"] for row in rows["principals"] if row["principal_type"] == "local_user")
    assert local.isdecimal() and local != ids["principals"]
    assert {row["id"] for row in rows["principals"]} >= {"idp:opaque", "local:initial-admin"}
    assert rows["documents"][0]["created_by"] == local
    assert rows["document_versions"][0]["document_id"] == doc
    assert rows["document_versions"][0]["source_object_key"] == "fixture"
    assert rows["audit_events"][0]["resource_id"] == doc
    assert rows["audit_events"][0]["subject"] == local
    assert rows["audit_events"][0]["details"]["document_id"] == doc
    assert rows["audit_events"][0]["details"]["text"] == ids["documents"]
    assert rows["audit_events"][0]["details"]["role_ids"] == [rows["roles"][0]["id"]]
    assert rows["event_outbox"][0]["payload"]["document_id"] == doc
    dead = rows["ingestion_dead_letters"][0]
    assert dead["event_id"].isdecimal() and dead["event_id"] == dead["payload"]["event_id"]
    assert rows["answer_feedback"][0]["answer_id"].isdecimal()
    assert rows["tenants"][0]["id"] == ids["tenants"]
    assert rows["tenants"][0]["knowledge_revision"] == 2
    database.rollback()
    tenant = "external-tenant-" + "x" * 80
    with database.begin():
        database.execute(metadata.tables["tenants"].insert().values(
            id=tenant, name="tenant", status="active", knowledge_revision=0, created_at=datetime(2026, 1, 1)))
        database.execute(metadata.tables["entity_aliases"].update().values(tenant_id=tenant))
        assert database.scalar(sa.text("SELECT tenant_id FROM entity_aliases")) == tenant
    for invalid in ["0", "01", "-1", "1.0", "9223372036854775808", "legacy-uuid"]:
        with pytest.raises(sa.exc.DBAPIError), database.begin():
            database.execute(sa.text("UPDATE documents SET id = :id"), {"id": invalid})
    with database.begin():
        database.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))


def test_allocation_failure_preserves_rows_and_constraints(database, monkeypatch):
    from app import global_ids
    ids = populate(database)
    before = sa.inspect(database).get_foreign_keys("documents")
    database.rollback()
    def unavailable(count):
        raise RuntimeError("authority unavailable")
    monkeypatch.setattr(global_ids, "new_ids", unavailable)
    with pytest.raises(RuntimeError, match="authority unavailable"):
        upgrade(database)
    assert database.scalar(sa.text("SELECT id FROM documents")) == ids["documents"]
    assert sa.inspect(database).get_foreign_keys("documents") == before
    database.rollback()


def test_empty_upgrade_never_calls_authority(database, monkeypatch):
    from app import global_ids
    monkeypatch.setattr(global_ids, "new_ids", lambda count: pytest.fail("empty migration allocated IDs"))
    upgrade(database)
    from app.db import Base, BusinessId
    inspector = sa.inspect(database)
    for table in Base.metadata.tables.values():
        actual = {column["name"]: column for column in inspector.get_columns(table.name)}
        checks = {check["name"] for check in inspector.get_check_constraints(table.name)}
        for column in table.c:
            if isinstance(column.type, BusinessId):
                assert actual[column.name]["type"].length == 19
                assert f"ck_{table.name}_{column.name}_business_id" in checks
    assert next(column for column in inspector.get_columns("entity_aliases") if column["name"] == "tenant_id")["type"].length == 128



@pytest.mark.parametrize("allocation", ["duplicate", "existing"])
def test_bad_authority_allocation_does_not_mutate(database, monkeypatch, allocation):
    from app import global_ids
    ids = populate(database)
    value = "1" if allocation == "duplicate" else ids["documents"]
    monkeypatch.setattr(global_ids, "new_ids", lambda count: [value] * count)
    with pytest.raises((RuntimeError, ValueError)):
        upgrade(database)
    assert database.scalar(sa.text("SELECT id FROM documents")) == ids["documents"]
    database.rollback()


def test_business_type_and_sqlite_constraint_reject_noncanonical_ids():
    from app.db import BusinessId
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table("ids", metadata, sa.Column("id", BusinessId(), primary_key=True),
                     sa.CheckConstraint("length(id) <= 19 AND CAST(id AS BIGINT) > 0 AND CAST(CAST(id AS BIGINT) AS VARCHAR) = id"))
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(table.insert().values(id="9223372036854775807"))
    for invalid in ["0", "01", "-1", "1.0", "9223372036854775808", "legacy-uuid"]:
        with pytest.raises(sa.exc.StatementError), engine.begin() as connection:
            connection.execute(table.insert().values(id=invalid))
        with pytest.raises(sa.exc.DBAPIError), engine.begin() as connection:
            connection.execute(sa.text("INSERT INTO ids (id) VALUES (:id)"), {"id": invalid})
    engine.dispose()


@pytest.mark.parametrize("fail", [False, True])
def test_administrator_principal_migration_preserves_config_key(database, monkeypatch, fail):
    from app import global_ids
    populate(database)
    monkeypatch.setattr(global_ids, "new_ids", lambda count: [str(10000 + i) for i in range(count)])
    upgrade(database)
    with database.begin():
        database.execute(sa.text("INSERT INTO local_admin_credentials (id, principal_id, username, password_hash, created_at) VALUES ('initial', 'local:initial-admin', 'admin', 'test-hash', now())"))
        database.execute(sa.text("UPDATE documents SET created_by = 'local:initial-admin'"))
        database.execute(sa.text("UPDATE audit_events SET subject = 'local:initial-admin', resource_type = 'principal', resource_id = 'local:initial-admin', details = :details"),
                         {"details": '{"principal_id":"local:initial-admin","principal_ids":["local:initial-admin","idp:opaque"],"text":"local:initial-admin"}'})
        database.execute(sa.text("UPDATE entity_aliases SET approved_by = 'local:initial-admin'"))
    spec = importlib.util.spec_from_file_location("admin_migration", ROOT / "migrations/versions/a2b3c4d5e6f7_authority_local_admin_ids.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    def allocate(count):
        if fail:
            raise RuntimeError("authority unavailable")
        return ["999999"] * count
    monkeypatch.setattr(global_ids, "new_ids", allocate)
    if fail:
        with pytest.raises(RuntimeError, match="authority unavailable"), database.begin(), Operations.context(MigrationContext.configure(database)):
            migration.upgrade()
        assert database.scalar(sa.text("SELECT principal_id FROM local_admin_credentials")) == "local:initial-admin"
    else:
        with database.begin(), Operations.context(MigrationContext.configure(database)):
            migration.upgrade()
        assert database.scalar(sa.text("SELECT id FROM principals WHERE principal_type = 'local_admin'")) == "999999"
        assert database.execute(sa.text("SELECT id, principal_id FROM local_admin_credentials")).one() == ("initial", "999999")
        assert database.scalar(sa.text("SELECT created_by FROM documents")) == "999999"
        assert database.scalar(sa.text("SELECT approved_by FROM entity_aliases")) == "999999"
        event = database.execute(sa.text("SELECT subject, resource_id, details FROM audit_events")).one()
        assert event.subject == event.resource_id == "999999"
        assert event.details == {"principal_id": "999999", "principal_ids": ["999999", "idp:opaque"], "text": "local:initial-admin"}
        assert database.scalar(sa.text("SELECT count(*) FROM principals WHERE id = 'idp:opaque'")) == 1
        checks = {check["name"] for check in sa.inspect(database).get_check_constraints("principals")}
        assert "ck_principals_local_business_id" in checks
        assert "ck_principals_local_user_business_id" not in checks
        database.rollback()
        with pytest.raises(sa.exc.DBAPIError), database.begin():
            database.execute(sa.text("UPDATE principals SET id = 'local:initial-admin' WHERE principal_type = 'local_admin'"))
    database.rollback()
