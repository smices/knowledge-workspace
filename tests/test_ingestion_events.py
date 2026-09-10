from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import db
from app.events import enqueue_document, enqueue_retry


def store():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


def document_fixture(session):
    session.add(db.Tenant(id="tenant", name="tenant"))
    session.add(db.Principal(id="owner", tenant_id="tenant"))
    document = db.Document(id="document", tenant_id="tenant", title="doc.txt", created_by="owner", version=1)
    version = db.DocumentVersion(
        id="version", document_id="document", version=1, source_object_key="tenant/source",
        source_filename="doc.txt", content_type="text/plain", source_hash="hash", created_by="owner",
    )
    session.add_all([document, version])
    session.flush()
    return document, version


def test_enqueue_document_commits_job_and_immutable_event_in_one_transaction():
    Session, engine = store()
    with Session() as session:
        document, version = document_fixture(session)
        payload = enqueue_document(session, document, version, trace_id="trace-1")
        session.commit()
        row = session.scalar(select(db.EventOutbox))
        job = session.scalar(select(db.IngestionJob))
        assert row.id == payload["event_id"]
        assert row.payload == payload
        assert payload["tenant_id"] == "tenant"
        assert payload["document_version_id"] == "version"
        assert payload["job_generation"] == 1
        assert job.generation == 1

        second = enqueue_document(session, document, version, trace_id="trace-2")
        session.commit()
        assert second["event_id"] != payload["event_id"]
        assert second["job_generation"] == 2
    engine.dispose()


def test_retry_outbox_preserves_original_event_id_without_raw_content():
    Session, engine = store()
    with Session() as session:
        document, version = document_fixture(session)
        payload = enqueue_document(session, document, version)
        enqueue_retry(session, payload, 5, "embedding_failed")
        session.commit()
        rows = session.scalars(select(db.EventOutbox).order_by(db.EventOutbox.created_at, db.EventOutbox.id)).all()
        assert len(rows) == 2
        assert rows[1].payload["event_id"] == payload["event_id"]
        assert "content" not in rows[1].payload
    engine.dispose()
