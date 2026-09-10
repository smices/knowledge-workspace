"""Durable, version-scoped document ingestion worker."""

import asyncio
import io
import json
import re
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid4, uuid5

from aiokafka import AIOKafkaConsumer
from qdrant_client import models
from pypdf import PdfReader
from sqlalchemy import select
from docx import Document as DocxDocument

from app.cache import bump_knowledge_revision
from app.config import settings
from app.db import (
    Chunk,
    Document,
    DocumentGrant,
    DocumentVersion,
    EntityAlias,
    IngestionDeadLetter,
    IngestionJob,
    KnowledgeRelation,
    Role,
    SessionLocal,
)
from app.events import enqueue_retry, publish_pending_outbox
from app.storage import get_file
from app.vector import client, ensure_collection, sparse_vector
from worker.llm import embed

MAX_BUSINESS_ATTEMPTS = 3
RETRY_DELAYS = (5, 30)
WORKER_ID = uuid4().hex
SUPPORTED_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/markdown",
}


def extract(content: bytes, content_type: str) -> list[tuple[str, list[str], int | None]]:
    if content_type == "application/pdf":
        return [(page.extract_text() or "", [], index + 1)
                for index, page in enumerate(PdfReader(io.BytesIO(content)).pages)]
    if content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        section, items = [], []
        for paragraph in DocxDocument(io.BytesIO(content)).paragraphs:
            text = paragraph.text.strip()
            if not text:
                continue
            match = re.search(r"\d+", paragraph.style.name) if paragraph.style.name.lower().startswith("heading") else None
            if match:
                level = int(match.group())
                section = section[:level - 1] + [text]
            else:
                items.append((text, list(section), None))
        return items
    if content_type not in {"text/plain", "text/markdown"}:
        raise ValueError("unsupported_content_type")
    return [(content.decode("utf-8", errors="replace").replace("\x00", " "), [], None)]


def chunks(segments: list[tuple[str, list[str], int | None]], size=900, overlap=120):
    if size <= overlap:
        raise ValueError("chunk size must be greater than overlap")
    result = []
    for text, section_path, page in segments:
        text = re.sub(r"\s+", " ", text).strip()
        result.extend((text[i:i + size], section_path, page)
                      for i in range(0, len(text), size - overlap) if text[i:i + size].strip())
    return result


ALIAS_PATTERN = re.compile(
    r"(?P<canonical>[A-Za-z][A-Za-z0-9_.-]{1,63}|[\u4e00-\u9fff]{2,8})\s*[（(]\s*"
    r"(?:简称|又名|别名|以下简称)\s*(?P<alias>[A-Za-z][A-Za-z0-9_.-]{1,63}|[\u4e00-\u9fff]{2,8})\s*[）)]"
)


def alias_candidates(pieces: list[tuple[str, list[str], int | None]]):
    seen = set()
    for chunk_index, (content, _, _) in enumerate(pieces):
        for match in ALIAS_PATTERN.finditer(content):
            canonical, alias = match.group("canonical"), match.group("alias")
            if canonical != alias and (canonical, alias) not in seen:
                seen.add((canonical, alias))
                yield chunk_index, canonical, alias, content[:600]


def validate_event(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("event_not_object")
    required = ("event_id", "tenant_id", "document_id", "document_version_id", "version",
                "event_type", "job_generation", "trace_id")
    text_limits = {
        "event_id": 36, "tenant_id": 128, "document_id": 36,
        "document_version_id": 36, "event_type": 128, "trace_id": 64,
    }
    for key, limit in text_limits.items():
        value = payload.get(key)
        invalid = (
            not isinstance(value, str)
            or not value.strip()
            or "\x00" in value
            or len(value) > limit
        )
        if not invalid:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                invalid = True
        if invalid:
            raise ValueError(f"event_{key}_invalid")
    if payload["event_type"] not in {"document.index", "document.delete"}:
        raise ValueError("event_type_not_supported")
    if isinstance(payload["version"], bool) or not isinstance(payload["version"], int) or payload["version"] < 1:
        raise ValueError("event_version_invalid")
    if isinstance(payload["job_generation"], bool) or not isinstance(payload["job_generation"], int) or payload["job_generation"] < 1:
        raise ValueError("event_generation_invalid")
    occurred_at = payload.get("occurred_at")
    if occurred_at is not None and (
        not isinstance(occurred_at, str)
        or not occurred_at
        or "\x00" in occurred_at
        or len(occurred_at) > 64
    ):
        raise ValueError("event_occurred_at_invalid")
    if occurred_at is not None:
        try:
            occurred_at.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("event_occurred_at_invalid") from exc
    return {key: payload[key] for key in required} | ({"occurred_at": occurred_at} if occurred_at else {})


def _safe_event_metadata(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {}
    metadata = {}

    def safe_text(value, limit):
        if not isinstance(value, str) or not value:
            return None
        return value.replace("\x00", "").encode("utf-8", "replace").decode("utf-8")[:limit] or None

    for key, limit in {
        "event_id": 36, "tenant_id": 128, "document_id": 36,
        "document_version_id": 36, "event_type": 128, "trace_id": 64,
    }.items():
        value = safe_text(payload.get(key), limit)
        if value is not None:
            metadata[key] = value
    for key in ("version", "job_generation"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2**31:
            metadata[key] = value
    occurred_at = payload.get("occurred_at")
    occurred_at = safe_text(occurred_at, 64)
    if occurred_at is not None:
        metadata["occurred_at"] = occurred_at
    return metadata


def _dead_letter(db, payload: dict | None, code: str, message: str):
    payload = _safe_event_metadata(payload)
    db.add(IngestionDeadLetter(
        event_id=payload.get("event_id"),
        event_type=payload.get("event_type"),
        tenant_id=payload.get("tenant_id"),
        document_id=payload.get("document_id"),
        document_version_id=payload.get("document_version_id"),
        trace_id=payload.get("trace_id"),
        error_code=code,
        # Persist only a controlled code; broker/storage exception strings can
        # contain source keys, endpoints, or credentials.
        error_message=code[:2000],
        payload={key: payload[key] for key in (
            "event_id", "tenant_id", "document_id", "document_version_id", "version",
            "event_type", "job_generation", "trace_id", "occurred_at"
        ) if key in payload},
    ))


def _safe_error(code: str, error: BaseException) -> str:
    return f"{code}:{type(error).__name__}"[:2000]


def _context(db, event, stage="index"):
    doc = db.scalar(select(Document).where(Document.id == event["document_id"]).with_for_update())
    version = db.scalar(select(DocumentVersion).where(
        DocumentVersion.id == event["document_version_id"], DocumentVersion.document_id == event["document_id"]
    ).with_for_update())
    job = db.scalar(select(IngestionJob).where(
        IngestionJob.document_version_id == event["document_version_id"], IngestionJob.stage == stage
    ).with_for_update())
    if doc is None or version is None or job is None:
        raise ValueError("ingestion_context_missing")
    return doc, version, job


def _obsolete(doc, version, job, event):
    return (
        doc.tenant_id != event["tenant_id"]
        or version.version != event["version"]
        or job.generation != event["job_generation"]
        or doc.version != version.version
        or doc.status in {"canceled", "deleted", "deleting"}
    )


def _claim_token():
    return f"{WORKER_ID}:{uuid4().hex}"


def _mark_processing(event):
    with SessionLocal() as db:
        doc, version, job = _context(db, event)
        if _obsolete(doc, version, job, event) or job.status in {"succeeded", "dead_letter", "obsolete"}:
            db.rollback()
            return None
        if job.attempt >= MAX_BUSINESS_ATTEMPTS:
            job.status = "dead_letter"
            version.status = "failed"
            doc.status = "failed"
            doc.error = "business_attempt_limit"
            _dead_letter(db, event, "business_attempt_limit", "business attempt limit reached")
            db.commit()
            return "dlq"
        token = _claim_token()
        job.status = "processing"
        job.worker_id = token
        job.attempt += 1
        job.started_at = datetime.utcnow()
        job.finished_at = None
        version.status = "processing"
        if doc.status not in {"processing", "queued"}:
            doc.status = "processing"
        db.commit()
        return token


def _mark_delete_processing(event):
    with SessionLocal() as db:
        doc, version, job = _context(db, event, stage="delete")
        if (
            doc.tenant_id != event["tenant_id"]
            or version.version != event["version"]
            or job.generation != event["job_generation"]
            or job.status in {"succeeded", "dead_letter", "obsolete"}
            or version.status == "deleted"
        ):
            db.rollback()
            return None
        if job.attempt >= MAX_BUSINESS_ATTEMPTS:
            job.status = "dead_letter"
            version.status = "failed"
            _dead_letter(db, event, "business_attempt_limit", "business attempt limit reached")
            db.commit()
            return "dlq"
        token = _claim_token()
        job.status = "processing"
        job.worker_id = token
        job.attempt += 1
        job.started_at = datetime.utcnow()
        db.commit()
        return token


def _roles(db, document_id: str, tenant_id: str):
    return [name for (name,) in db.execute(
        select(Role.name).join(DocumentGrant, DocumentGrant.role_id == Role.id).where(
            DocumentGrant.document_id == document_id, Role.tenant_id == tenant_id
        )
    ).all()]


def _version_filter(tenant_id: str, version_id: str):
    return models.Filter(must=[
        models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id)),
        models.FieldCondition(key="document_version_id", match=models.MatchValue(value=version_id)),
    ])


def _publish_index(event, pieces, vectors, loader=None, qdrant=None, worker_id=None):
    qdrant = qdrant or client
    with SessionLocal() as db:
        doc, version, job = _context(db, event)
        if (
            _obsolete(doc, version, job, event)
            or job.status in {"succeeded", "dead_letter", "obsolete"}
            or (worker_id is not None and job.worker_id != worker_id)
        ):
            db.rollback()
            return "obsolete"
        if len(pieces) != len(vectors) or not pieces:
            raise ValueError("embedding_count_mismatch")
        if any(not isinstance(vector, list) or len(vector) != settings.embedding_dimensions for vector in vectors):
            raise ValueError("embedding_dimension_mismatch")
        roles = _roles(db, doc.id, doc.tenant_id)
        db.query(Chunk).filter(Chunk.document_version_id == version.id).delete(synchronize_session=False)
        db.query(KnowledgeRelation).filter(KnowledgeRelation.document_version_id == version.id).delete(synchronize_session=False)
        db.query(EntityAlias).filter(EntityAlias.document_version_id == version.id).delete(synchronize_session=False)
        db.add_all([
            Chunk(
                id=str(uuid5(NAMESPACE_URL, f"{version.id}:{index}")),
                document_version_id=version.id,
                chunk_index=index,
                content=piece,
                content_hash=sha256(piece.encode()).hexdigest(),
                section_path=section_path,
                page=page,
            )
            for index, (piece, section_path, page) in enumerate(pieces)
        ])
        db.add_all([
            EntityAlias(
                tenant_id=doc.tenant_id, document_version_id=version.id, chunk_index=index,
                canonical=canonical, alias=alias, excerpt=excerpt,
                source="pattern", created_by=doc.created_by,
            )
            for index, canonical, alias, excerpt in alias_candidates(pieces)
        ])
        qdrant.delete(settings.qdrant_collection, models.FilterSelector(filter=_version_filter(doc.tenant_id, version.id)))
        qdrant.upsert(settings.qdrant_collection, [
            models.PointStruct(
                id=str(uuid5(NAMESPACE_URL, f"{version.id}:{index}")),
                vector={"": vector, "lexical": sparse_vector(piece)},
                payload={
                    "tenant_id": doc.tenant_id,
                    "document_id": doc.id,
                    "document_version_id": version.id,
                    "document_version": version.version,
                    "title": doc.title,
                    "content": piece,
                    "allowed_roles": roles,
                    "source_uri": version.source_object_key,
                    "chunk_index": index,
                    "section_path": section_path,
                    "page": page,
                },
            )
            for index, ((piece, section_path, page), vector) in enumerate(zip(pieces, vectors))
        ])
        bump_knowledge_revision(doc.tenant_id, db)
        doc.status = "ready"
        doc.error = None
        version.status = "ready"
        version.error_code = None
        version.error_message = None
        version.published_at = datetime.now(UTC).replace(tzinfo=None)
        job.status = "succeeded"
        job.finished_at = datetime.utcnow()
        db.commit()
        return "success"


def _delete_version(event, worker_id=None, qdrant=None):
    qdrant = qdrant or client
    with SessionLocal() as db:
        doc, version, job = _context(db, event, stage="delete")
        if (
            doc.tenant_id != event["tenant_id"]
            or version.version != event["version"]
            or job.generation != event["job_generation"]
            or (worker_id is not None and job.worker_id != worker_id)
        ):
            db.rollback()
            return "obsolete"
        if job.status in {"dead_letter", "obsolete"} or (version.status == "deleted" and job.status == "succeeded"):
            db.rollback()
            return "obsolete"
        qdrant_filter = _version_filter(doc.tenant_id, version.id)
        qdrant.delete(settings.qdrant_collection, models.FilterSelector(filter=qdrant_filter))
        db.query(Chunk).filter(Chunk.document_version_id == version.id).delete(synchronize_session=False)
        db.query(KnowledgeRelation).filter(KnowledgeRelation.document_version_id == version.id).delete(synchronize_session=False)
        db.query(EntityAlias).filter(EntityAlias.document_version_id == version.id).delete(synchronize_session=False)
        version.status = "deleted"
        job.status = "succeeded"
        job.finished_at = datetime.utcnow()
        remaining = db.scalar(select(DocumentVersion.id).where(
            DocumentVersion.document_id == doc.id, DocumentVersion.status != "deleted"
        ))
        if remaining is None:
            doc.status = "deleted"
            doc.deleted_at = datetime.utcnow()
        bump_knowledge_revision(doc.tenant_id, db)
        db.commit()
        return "success"


def _record_failure(event, code, error, worker_id=None):
    with SessionLocal() as db:
        stage = "delete" if event.get("event_type") == "document.delete" else "index"
        try:
            doc, version, job = _context(db, event, stage=stage)
        except ValueError as exc:
            if str(exc) != "ingestion_context_missing":
                raise
            _dead_letter(db, event, code, str(error))
            db.commit()
            return "dlq"
        stale = _obsolete(doc, version, job, event) if stage == "index" else (
            doc.tenant_id != event["tenant_id"]
            or version.version != event["version"]
            or job.generation != event["job_generation"]
            or doc.status == "deleted"
        )
        if (
            stale
            or job.status in {"succeeded", "dead_letter", "obsolete"}
            or (worker_id is not None and job.worker_id != worker_id)
            or (worker_id is None and job.status == "processing")
        ):
            db.rollback()
            return "obsolete"
        message = _safe_error(code, error)
        job.error_code = code
        job.error_message = message
        version.error_code = code
        version.error_message = message
        if job.attempt >= MAX_BUSINESS_ATTEMPTS:
            job.status = "dead_letter"
            version.status = "failed"
            if stage == "index":
                doc.status = "failed"
                doc.error = message
            _dead_letter(db, event, code, message)
            db.commit()
            return "dlq"
        job.status = "retry"
        version.status = "retrying"
        delay = RETRY_DELAYS[min(job.attempt - 1, len(RETRY_DELAYS) - 1)]
        enqueue_retry(db, event, delay, message)
        db.commit()
        return "retry"


def handle_event(payload: dict, embedder=None, loader=None):
    event = validate_event(payload)
    if event["event_type"] == "document.delete":
        try:
            worker_id = _mark_delete_processing(event)
        except ValueError as exc:
            if str(exc) == "ingestion_context_missing":
                return _record_failure(event, "ingestion_context_missing", exc)
            raise
        if worker_id == "dlq":
            return "dlq"
        if worker_id is None:
            return "obsolete"
        try:
            return _delete_version(event, worker_id)
        except Exception as exc:
            return _record_failure(event, "deletion_failed", exc, worker_id)
    try:
        worker_id = _mark_processing(event)
    except ValueError as exc:
        if str(exc) == "ingestion_context_missing":
            return _record_failure(event, "ingestion_context_missing", exc)
        raise
    if worker_id == "dlq":
        return "dlq"
    if worker_id is None:
        return "obsolete"
    try:
        embedder = embedder or embed
        loader = loader or get_file
        with SessionLocal() as db:
            version = db.get(DocumentVersion, event["document_version_id"])
            if version is None:
                raise ValueError("document_version_missing")
            source_key, content_type = version.source_object_key, version.content_type
        if content_type not in SUPPORTED_TYPES:
            raise ValueError("unsupported_content_type")
        pieces = chunks(extract(loader(source_key), content_type))
        if not pieces:
            raise ValueError("empty_content")
        vectors = []
        for start in range(0, len(pieces), 32):
            vectors.extend(embedder([piece for piece, _, _ in pieces[start:start + 32]]))
        return _publish_index(event, pieces, vectors, worker_id=worker_id)
    except Exception as exc:
        return _record_failure(event, "ingestion_failed", exc, worker_id)


def handle_message(value: bytes):
    try:
        payload = json.loads(value)
        event = validate_event(payload)
    except Exception as exc:
        with SessionLocal() as db:
            _dead_letter(db, payload if isinstance(locals().get("payload"), dict) else None,
                         "malformed_event", str(exc))
            db.commit()
        return "dlq"
    return handle_event(event)


async def relay_loop(stop: asyncio.Event):
    while not stop.is_set():
        try:
            await publish_pending_outbox()
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except asyncio.TimeoutError:
            continue


async def run():
    ensure_collection()
    consumer = AIOKafkaConsumer(
        settings.kafka_document_topic,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id="knowledge-indexer",
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        max_poll_records=1,
        max_poll_interval_ms=1_800_000,
    )
    stop = asyncio.Event()
    relay = asyncio.create_task(relay_loop(stop))
    try:
        await consumer.start()
        async for message in consumer:
            outcome = await asyncio.to_thread(handle_message, message.value)
            if outcome in {"success", "obsolete", "retry", "dlq"}:
                await consumer.commit()
    finally:
        stop.set()
        relay.cancel()
        try:
            await relay
        except asyncio.CancelledError:
            pass
        await consumer.stop()


if __name__ == "__main__":
    asyncio.run(run())
