import io, json, re
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5
from aiokafka import AIOKafkaConsumer
from aiokafka.errors import CommitFailedError
from qdrant_client import models
from sqlalchemy import select
from app.cache import bump_knowledge_revision
from app.config import settings
from app.db import Chunk, Document, DocumentGrant, DocumentVersion, Role, SessionLocal
from app.storage import get_file
from app.vector import client, ensure_collection
from worker.llm import embed
from pypdf import PdfReader


def extract(content: bytes, content_type: str) -> str:
    if content_type == "application/pdf":
        return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages)
    return content.decode("utf-8", errors="replace").replace("\x00", " ")

def chunks(text: str, size=900, overlap=120):
    text = re.sub(r"\s+", " ", text).strip()
    return [text[i:i + size] for i in range(0, len(text), size - overlap) if text[i:i + size].strip()]

async def run():
    ensure_collection()
    consumer = AIOKafkaConsumer(settings.kafka_document_topic,
        bootstrap_servers=settings.kafka_bootstrap_servers, group_id="knowledge-indexer",
        enable_auto_commit=False, auto_offset_reset="earliest",
        max_poll_records=1, max_poll_interval_ms=1800000)
    await consumer.start()
    try:
        async for message in consumer:
            document_id = json.loads(message.value)["document_id"]
            with SessionLocal() as db:
                doc = db.get(Document, document_id)
                # A process crash can leave the document in processing; Kafka redelivery
                # must be allowed to recover that work instead of permanently skipping it.
                if not doc or doc.status in {"ready", "canceled", "deleted"}:
                    try:
                        await consumer.commit()
                    except CommitFailedError:
                        pass
                    continue
                doc.status = "processing"; db.commit()
                try:
                    text = extract(get_file(doc.object_key), doc.content_type)
                    pieces = chunks(text)
                    vectors = []
                    for start in range(0, len(pieces), 32):
                        vectors.extend(embed(pieces[start:start + 32]))
                    db.refresh(doc)
                    if doc.status not in {"canceled", "deleted"}:
                        version = db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == doc.id).order_by(DocumentVersion.version.desc()))
                        if version:
                            db.query(Chunk).filter(Chunk.document_version_id == version.id).delete(synchronize_session=False)
                            db.add_all([Chunk(id=str(uuid5(NAMESPACE_URL, f"{version.id}:{i}")), document_version_id=version.id,
                                              chunk_index=i, content=piece, content_hash=sha256(piece.encode()).hexdigest())
                                        for i, piece in enumerate(pieces)])
                        roles = [
                            name
                            for (name,) in db.query(Role.name)
                            .join(DocumentGrant, DocumentGrant.role_id == Role.id)
                            .filter(DocumentGrant.document_id == doc.id)
                            .all()
                        ]
                        client.delete(settings.qdrant_collection, models.FilterSelector(filter=models.Filter(must=[models.FieldCondition(key="document_id", match=models.MatchValue(value=doc.id))])))
                        client.upsert(settings.qdrant_collection, [models.PointStruct(id=str(uuid5(NAMESPACE_URL, f"{doc.id}:{i}")), vector=v, payload={"tenant_id": doc.tenant_id, "document_id": doc.id, "title": doc.title, "content": piece, "allowed_roles": roles, "source_uri": doc.object_key, "chunk_index": i}) for i, (piece, v) in enumerate(zip(pieces, vectors))])
                        bump_knowledge_revision(doc.tenant_id)
                        doc.status = "ready"; doc.error = None
                except Exception as exc:
                    doc.status = "failed"; doc.error = str(exc)[:2000]
                db.commit()
                try:
                    await consumer.commit()
                except CommitFailedError:
                    # The database state is durable; Kafka will redeliver after a rebalance.
                    continue
    finally: await consumer.stop()

if __name__ == "__main__":
    import asyncio; asyncio.run(run())
