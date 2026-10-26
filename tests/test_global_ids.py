import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import db, global_ids
from app.events import enqueue_document


def test_global_id_client_requires_snowflake_v2_and_preserves_int64_strings():
    assert global_ids._SnowflakeV2Client._validate(
        {"algorithm": "snowflake-v2", "ids": ["9223372036854775807"]}, 1
    ) == ["9223372036854775807"]
    with pytest.raises(RuntimeError):
        global_ids._SnowflakeV2Client._validate(
            {"algorithm": "snowflake-v1", "ids": ["1"]}, 1
        )
    with pytest.raises(RuntimeError):
        global_ids._SnowflakeV2Client._validate(
            {"algorithm": "snowflake-v2", "ids": ["9223372036854775808"]}, 1
        )


def test_global_id_batches_large_allocations_and_rejects_noncanonical_ids(monkeypatch):
    class Authority:
        def __init__(self):
            self.counts = []
            self.value = 0

        def next(self, count):
            self.counts.append(count)
            values = [str(self.value + index + 1) for index in range(count)]
            self.value += count
            return values

    authority = Authority()
    monkeypatch.setattr(global_ids, "_client", authority)
    ids = global_ids.new_ids(1001)
    assert authority.counts == [1000, 1]
    assert ids == [str(value) for value in range(1, 1002)]
    assert global_ids.validate_id("9223372036854775807") == "9223372036854775807"
    for value in ("0", "01", "+1", "1.0", "9223372036854775808",
                  "123e4567-e89b-12d3-a456-426614174000", 1):
        with pytest.raises(ValueError):
            global_ids.validate_id(value)

    class RepeatingAuthority:
        def next(self, count):
            return ["7"] * count

    monkeypatch.setattr(global_ids, "_client", RepeatingAuthority())
    with pytest.raises(global_ids.GlobalIDUnavailable):
        global_ids.new_ids(1001)


def test_business_primary_keys_are_global_id_strings_without_database_defaults():
    for table in (db.Role.__table__, db.Permission.__table__, db.KnowledgeBase.__table__, db.Document.__table__, db.DocumentVersion.__table__,
                  db.Chunk.__table__, db.IngestionJob.__table__, db.EventOutbox.__table__,
                  db.IngestionDeadLetter.__table__, db.AuditEvent.__table__, db.QueryEvent.__table__,
                  db.AnswerFeedback.__table__, db.KnowledgeRelation.__table__, db.EntityAlias.__table__):
        column = table.c.id
        assert column.type.length == 19
        assert column.server_default is None

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        session.add(db.Tenant(id="tenant", name="tenant"))
        session.add(db.Principal(id="owner", tenant_id="tenant"))
        document = db.Document(tenant_id="tenant", title="doc", created_by="owner")
        session.add(document)
        session.flush()
        document_id = document.id
        session.commit()
    with Session() as session:
        persisted = session.get(db.Document, document_id)
        assert persisted.id.isdecimal() and 0 < int(persisted.id) <= 9223372036854775807
    engine.dispose()


def test_global_id_failure_rolls_back_event_and_job(monkeypatch):
    class BrokenAuthority:
        def next(self, _count=1):
            raise RuntimeError("unavailable")

    monkeypatch.setattr(global_ids, "_client", BrokenAuthority())
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        session.add_all([db.Tenant(id="tenant", name="tenant"), db.Principal(id="owner", tenant_id="tenant")])
        document = db.Document(id="501", tenant_id="tenant", title="doc", created_by="owner")
        version = db.DocumentVersion(id="502", document_id="501", version=1,
                                     source_object_key="source", source_filename="doc",
                                     content_type="text/plain", source_hash="hash", created_by="owner")
        session.add_all([document, version])
        session.commit()
        with pytest.raises(global_ids.GlobalIDUnavailable):
            enqueue_document(session, document, version)
        session.rollback()
        assert session.scalar(select(db.IngestionJob)) is None
        assert session.scalar(select(db.EventOutbox)) is None
    engine.dispose()
