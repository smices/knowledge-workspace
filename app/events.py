"""Transactional ingestion outbox and its Kafka relay."""

from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

from aiokafka import AIOKafkaProducer
from sqlalchemy import or_, select

from app.config import settings
from app.db import EventOutbox, IngestionJob, SessionLocal

OUTBOX_LEASE_SECONDS = 60
RETRY_BASE_SECONDS = 5


def enqueue_document(db, document, version, event_type="document.index", trace_id=None):
    """Add the job and immutable event to the caller's transaction."""
    if event_type not in {"document.index", "document.delete"}:
        raise ValueError("unsupported ingestion event type")
    if trace_id is not None and (not isinstance(trace_id, str) or not trace_id or len(trace_id) > 64):
        raise ValueError("invalid trace id")
    if document.status in {"deleted", "deleting"} and event_type != "document.delete":
        raise ValueError("document is no longer mutable")
    if version.document_id != document.id:
        raise ValueError("document version does not belong to document")
    stage = "delete" if event_type == "document.delete" else "index"
    job = db.scalar(
        select(IngestionJob)
        .where(IngestionJob.document_version_id == version.id, IngestionJob.stage == stage)
        .with_for_update()
    )
    if job is None:
        job = IngestionJob(document_version_id=version.id, stage=stage, generation=1)
        db.add(job)
        db.flush()
    else:
        job.generation += 1
        job.attempt = 0
        job.status = "queued"
        job.worker_id = None
        job.started_at = None
        job.error_code = None
        job.error_message = None
        job.finished_at = None
    event_id = str(uuid4())
    payload = {
        "event_id": event_id,
        "tenant_id": document.tenant_id,
        "document_id": document.id,
        "document_version_id": version.id,
        "version": version.version,
        "event_type": event_type,
        "job_generation": job.generation,
        "trace_id": trace_id or str(uuid4()),
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    job.trace_id = payload["trace_id"]
    db.add(EventOutbox(
        id=event_id,
        event_type=event_type,
        aggregate_type="document",
        aggregate_id=document.id,
        payload=payload,
    ))
    db.flush()
    return payload


def enqueue_retry(db, payload: dict, delay_seconds: int, error: str):
    """Persist a redelivery with the original immutable event id."""
    retry_id = str(uuid4())
    db.add(EventOutbox(
        id=retry_id,
        event_type=payload.get("event_type", "document.index"),
        aggregate_type="document",
        aggregate_id=payload.get("document_id", "unknown"),
        payload=dict(payload),
        status="retry",
        available_at=datetime.utcnow() + timedelta(seconds=delay_seconds),
        # Keep broker metadata free of arbitrary exception/source strings.
        last_error="retry",
    ))
    db.flush()
    return retry_id


def _claim_pending(limit: int):
    now = datetime.utcnow()
    with SessionLocal() as db:
        rows = db.scalars(
            select(EventOutbox)
            .where(
                or_(
                    EventOutbox.status.in_(["pending", "retry"]),
                    EventOutbox.status == "publishing",
                ),
                EventOutbox.available_at <= now,
            )
            .order_by(EventOutbox.created_at, EventOutbox.id)
            .with_for_update(skip_locked=True)
            .limit(limit)
        ).all()
        claimed = []
        for row in rows:
            row.status = "publishing"
            row.attempts += 1
            row.available_at = now + timedelta(seconds=OUTBOX_LEASE_SECONDS)
            claimed.append((row.id, row.event_type, row.aggregate_id, dict(row.payload), row.attempts))
        db.commit()
        return claimed


def _finish_outbox(row_id, *, error=None):
    with SessionLocal() as db:
        row = db.get(EventOutbox, row_id)
        if row is None:
            return
        if error is None:
            row.status = "published"
            row.published_at = datetime.utcnow()
            row.last_error = None
        else:
            row.status = "retry"
            row.available_at = datetime.utcnow() + timedelta(seconds=RETRY_BASE_SECONDS * min(row.attempts, 8))
            row.last_error = error[:2000]
        db.commit()


async def publish_pending_outbox(limit=100):
    """Relay due outbox rows; broker acknowledgement precedes the DB mark."""
    producer = AIOKafkaProducer(bootstrap_servers=settings.kafka_bootstrap_servers)
    await producer.start()
    published = 0
    try:
        for row_id, _event_type, aggregate_id, payload, _attempt in _claim_pending(limit):
            try:
                tenant = payload.get("tenant_id", "unknown")
                document = payload.get("document_id", aggregate_id)
                await producer.send_and_wait(
                    settings.kafka_document_topic,
                    json.dumps(payload, ensure_ascii=False).encode(),
                    key=f"{tenant}:{document}".encode(),
                )
            except Exception as exc:
                _finish_outbox(row_id, error=type(exc).__name__)
            else:
                _finish_outbox(row_id)
                published += 1
    finally:
        await producer.stop()
    return published


async def publish_documents(document_ids: list[str]):
    """Compatibility publisher for old callers; new mutations use the outbox."""
    producer = AIOKafkaProducer(bootstrap_servers=settings.kafka_bootstrap_servers)
    await producer.start()
    try:
        for document_id in document_ids:
            payload = {"event_id": str(uuid4()), "document_id": document_id, "event_type": "document.index"}
            await producer.send_and_wait(
                settings.kafka_document_topic,
                json.dumps(payload).encode(),
                key=document_id.encode(),
            )
    finally:
        await producer.stop()


async def publish_document(document_id: str):
    await publish_documents([document_id])
