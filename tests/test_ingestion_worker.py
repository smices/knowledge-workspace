import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import db
from app.events import enqueue_document
from app.config import settings
from worker import main as worker


class FakeQdrant:
    def __init__(self):
        self.deleted = []
        self.upserted = []

    def delete(self, *args, **kwargs):
        self.deleted.append((args, kwargs))

    def upsert(self, *args, **kwargs):
        self.upserted.append((args, kwargs))


def setup(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", Session)
    monkeypatch.setattr(settings, "embedding_dimensions", 2)
    with Session() as session:
        session.add_all([db.Tenant(id="tenant", name="tenant"), db.Principal(id="owner", tenant_id="tenant")])
        document = db.Document(id="document", tenant_id="tenant", title="doc.txt", created_by="owner", version=1, status="queued")
        version = db.DocumentVersion(
            id="version", document_id="document", version=1, source_object_key="tenant/source",
            source_filename="doc.txt", content_type="text/plain", source_hash="hash", created_by="owner",
        )
        session.add_all([document, version])
        session.flush()
        payload = enqueue_document(session, document, version)
        session.commit()
    return Session, engine, payload


def test_worker_publishes_only_complete_embedding_batch(monkeypatch):
    Session, engine, payload = setup(monkeypatch)
    qdrant = FakeQdrant()
    monkeypatch.setattr(worker, "client", qdrant)
    result = worker.handle_event(
        payload,
        embedder=lambda texts: [[0.1, 0.2] for _ in texts],
        loader=lambda key: b"first paragraph\nsecond paragraph",
    )
    assert result == "success"
    assert len(qdrant.upserted) == 1
    with Session() as session:
        document = session.get(db.Document, "document")
        version = session.get(db.DocumentVersion, "version")
        assert document.status == "ready"
        assert version.status == "ready"
        assert session.scalar(select(db.Chunk).where(db.Chunk.document_version_id == "version")) is not None
    engine.dispose()


def test_failure_recording_database_outage_is_not_an_ackable_dead_letter(monkeypatch):
    Session, engine, payload = setup(monkeypatch)
    token = worker._mark_processing(payload)
    def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic database outage")
    monkeypatch.setattr(worker, "_context", unavailable)
    with pytest.raises(RuntimeError, match="synthetic database outage"):
        worker._record_failure(payload, "ingestion_failed", RuntimeError("model failed"), token)
    with Session() as session:
        job = session.scalar(select(db.IngestionJob))
        assert job.status == "processing" and job.worker_id == token
        assert session.scalar(select(db.IngestionDeadLetter)) is None
        assert session.scalar(select(db.EventOutbox).where(db.EventOutbox.status == "retry")) is None
    engine.dispose()


def test_partial_embeddings_persist_retry_then_dead_letter(monkeypatch):
    Session, engine, payload = setup(monkeypatch)
    for _ in range(3):
        assert worker.handle_event(payload, embedder=lambda texts: [], loader=lambda key: b"content") in {"retry", "dlq"}
    with Session() as session:
        job = session.scalar(select(db.IngestionJob))
        assert job.status == "dead_letter"
        assert session.scalar(select(db.IngestionDeadLetter)) is not None
        assert session.scalar(select(db.EventOutbox).where(db.EventOutbox.status == "retry")) is not None
    engine.dispose()


def test_obsolete_and_canceled_events_do_not_embed(monkeypatch):
    Session, engine, payload = setup(monkeypatch)
    with Session() as session:
        document = session.get(db.Document, "document")
        version = session.get(db.DocumentVersion, "version")
        newer = enqueue_document(session, document, version)
        document.status = "canceled"
        session.commit()
    called = False

    def embedder(texts):
        nonlocal called
        called = True
        return [[0.1, 0.2] for _ in texts]

    assert worker.handle_event(payload, embedder=embedder, loader=lambda key: b"content") == "obsolete"
    assert not called
    assert newer["job_generation"] == 2
    engine.dispose()


def test_delete_event_tombstones_metadata_and_qdrant_by_version(monkeypatch):
    Session, engine, payload = setup(monkeypatch)
    qdrant = FakeQdrant()
    monkeypatch.setattr(worker, "client", qdrant)
    with Session() as session:
        document = session.get(db.Document, "document")
        version = session.get(db.DocumentVersion, "version")
        document.status = "deleting"
        delete_event = enqueue_document(session, document, version, event_type="document.delete")
        session.commit()
    assert worker.handle_event(delete_event) == "success"
    with Session() as session:
        assert session.get(db.Document, "document").status == "deleted"
        assert session.get(db.DocumentVersion, "version").status == "deleted"
    filter_repr = repr(qdrant.deleted[0])
    assert "document_version_id" in filter_repr
    engine.dispose()


def test_processing_claim_is_reclaimed_after_worker_crash(monkeypatch):
    Session, engine, payload = setup(monkeypatch)
    qdrant = FakeQdrant()
    monkeypatch.setattr(worker, "client", qdrant)
    first_claim = worker._mark_processing(payload)
    assert first_claim
    assert worker.handle_event(
        payload,
        embedder=lambda texts: [[0.1, 0.2] for _ in texts],
        loader=lambda key: b"recovered content",
    ) == "success"
    with Session() as session:
        job = session.scalar(select(db.IngestionJob))
        assert job.status == "succeeded"
        assert job.attempt == 2
        assert job.worker_id != first_claim
    engine.dispose()


def test_stale_failure_does_not_obsolete_new_generation(monkeypatch):
    Session, engine, payload = setup(monkeypatch)
    first_claim = worker._mark_processing(payload)
    with Session() as session:
        document = session.get(db.Document, "document")
        version = session.get(db.DocumentVersion, "version")
        replacement = enqueue_document(session, document, version)
        session.commit()
    assert replacement["job_generation"] == 2
    assert worker._record_failure(payload, "old_worker", RuntimeError("late"), first_claim) == "obsolete"
    with Session() as session:
        job = session.scalar(select(db.IngestionJob))
        assert job.generation == 2
        assert job.status == "queued"
    engine.dispose()


def test_malformed_event_is_sanitized_into_dlq(monkeypatch):
    Session, engine, _ = setup(monkeypatch)
    malformed = {"event_id": [], "tenant_id": {"secret": "x"}, "version": True}
    assert worker.handle_message(json.dumps(malformed).encode()) == "dlq"
    with Session() as session:
        row = session.scalar(select(db.IngestionDeadLetter))
        assert row is not None
        assert row.event_id is None
        assert row.payload == {}
        assert "secret" not in str(row.payload)
    engine.dispose()


def test_nul_in_malformed_event_is_removed_before_dlq_insert(monkeypatch):
    Session, engine, _ = setup(monkeypatch)
    malformed = {"event_id": "bad\x00value", "tenant_id": "tenant", "version": True}
    assert worker.handle_message(json.dumps(malformed).encode()) == "dlq"
    with Session() as session:
        row = session.scalar(select(db.IngestionDeadLetter))
        assert row is not None
        assert "\x00" not in (row.event_id or "")
    engine.dispose()


def _ambiguous_commit_factory(Session):
    state = {"raised": False}

    class SessionProxy:
        def __init__(self):
            self.inner = Session()

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

        def commit(self):
            result = self.inner.commit()
            if not state["raised"]:
                state["raised"] = True
                raise RuntimeError("commit acknowledgement uncertain")
            return result

        def __getattr__(self, name):
            return getattr(self.inner, name)

    return SessionProxy


def test_ambiguous_index_claim_commits_durable_retry(monkeypatch):
    Session, engine, payload = setup(monkeypatch)
    monkeypatch.setattr(worker, "SessionLocal", _ambiguous_commit_factory(Session))
    with pytest.raises(RuntimeError, match="acknowledgement uncertain"):
        worker.handle_event(
            payload,
            embedder=lambda texts: [[0.1, 0.2] for _ in texts],
            loader=lambda key: b"never reached",
        )
    with Session() as session:
        job = session.scalar(select(db.IngestionJob))
        assert job.status == "processing"
        assert session.scalar(select(db.EventOutbox).where(db.EventOutbox.status == "retry")) is None
    monkeypatch.setattr(worker, "SessionLocal", Session)
    monkeypatch.setattr(worker, "client", FakeQdrant())
    assert worker.handle_event(
        payload,
        embedder=lambda texts: [[0.1, 0.2] for _ in texts],
        loader=lambda key: b"redelivered content",
    ) == "success"
    engine.dispose()


def test_ambiguous_delete_claim_commits_durable_retry(monkeypatch):
    Session, engine, payload = setup(monkeypatch)
    with Session() as session:
        document = session.get(db.Document, "document")
        version = session.get(db.DocumentVersion, "version")
        document.status = "deleting"
        delete_event = enqueue_document(session, document, version, event_type="document.delete")
        session.commit()
    monkeypatch.setattr(worker, "SessionLocal", _ambiguous_commit_factory(Session))
    with pytest.raises(RuntimeError, match="acknowledgement uncertain"):
        worker.handle_event(delete_event)
    with Session() as session:
        job = session.scalar(select(db.IngestionJob).where(db.IngestionJob.stage == "delete"))
        assert job.status == "processing"
        assert session.scalar(select(db.EventOutbox).where(db.EventOutbox.status == "retry")) is None
    monkeypatch.setattr(worker, "SessionLocal", Session)
    monkeypatch.setattr(worker, "client", FakeQdrant())
    assert worker.handle_event(delete_event) == "success"
    engine.dispose()


def test_delete_failure_attempts_are_persisted_before_qdrant(monkeypatch):
    Session, engine, payload = setup(monkeypatch)

    class BrokenQdrant(FakeQdrant):
        def delete(self, *args, **kwargs):
            raise RuntimeError("qdrant down")

    qdrant = BrokenQdrant()
    monkeypatch.setattr(worker, "client", qdrant)
    with Session() as session:
        document = session.get(db.Document, "document")
        version = session.get(db.DocumentVersion, "version")
        document.status = "deleting"
        delete_event = enqueue_document(session, document, version, event_type="document.delete")
        session.commit()
    assert worker.handle_event(delete_event) == "retry"
    assert worker.handle_event(delete_event) == "retry"
    assert worker.handle_event(delete_event) == "dlq"
    with Session() as session:
        job = session.scalar(select(db.IngestionJob).where(db.IngestionJob.stage == "delete"))
        assert job.attempt == 3
        assert job.status == "dead_letter"
    engine.dispose()
