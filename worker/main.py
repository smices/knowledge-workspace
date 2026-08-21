import io, json, re
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5
from aiokafka import AIOKafkaConsumer
from aiokafka.errors import CommitFailedError
from qdrant_client import models
from sqlalchemy import select
from app.cache import bump_knowledge_revision
from app.config import settings
from app.db import Chunk, Document, DocumentGrant, DocumentVersion, KnowledgeRelation, Role, SessionLocal
from app.storage import get_file
from app.vector import client, ensure_collection, sparse_vector
from worker.llm import embed
from pypdf import PdfReader
from docx import Document as DocxDocument


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
    return [(content.decode("utf-8", errors="replace").replace("\x00", " "), [], None)]

def chunks(segments: list[tuple[str, list[str], int | None]], size=900, overlap=120):
    result = []
    for text, section_path, page in segments:
        text = re.sub(r"\s+", " ", text).strip()
        result.extend((text[i:i + size], section_path, page)
                      for i in range(0, len(text), size - overlap) if text[i:i + size].strip())
    return result

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
                    pieces = chunks(extract(get_file(doc.object_key), doc.content_type))
                    if not pieces:
                        raise RuntimeError("no extractable text")
                    vectors = []
                    for start in range(0, len(pieces), 32):
                        vectors.extend(embed([piece for piece, _, _ in pieces[start:start + 32]]))
                    db.refresh(doc)
                    if doc.status not in {"canceled", "deleted"}:
                        version = db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == doc.id).order_by(DocumentVersion.version.desc()))
                        if version is None:
                            raise RuntimeError("document version missing")
                        db.query(Chunk).filter(Chunk.document_version_id == version.id).delete(synchronize_session=False)
                        db.query(KnowledgeRelation).filter(KnowledgeRelation.document_version_id == version.id).delete(synchronize_session=False)
                        db.add_all([Chunk(id=str(uuid5(NAMESPACE_URL, f"{version.id}:{i}")), document_version_id=version.id,
                                          chunk_index=i, content=piece, content_hash=sha256(piece.encode()).hexdigest(),
                                          section_path=section_path, page=page)
                                    for i, (piece, section_path, page) in enumerate(pieces)])
                        roles = [
                            name
                            for (name,) in db.query(Role.name)
                            .join(DocumentGrant, DocumentGrant.role_id == Role.id)
                            .filter(DocumentGrant.document_id == doc.id)
                            .all()
                        ]
                        client.delete(settings.qdrant_collection, models.FilterSelector(filter=models.Filter(must=[models.FieldCondition(key="document_id", match=models.MatchValue(value=doc.id))])))
                        client.upsert(settings.qdrant_collection, [models.PointStruct(
                            id=str(uuid5(NAMESPACE_URL, f"{version.id}:{i}")),
                            vector={"": v, "lexical": sparse_vector(piece)},
                            payload={"tenant_id": doc.tenant_id, "document_id": doc.id,
                                     "document_version": version.version, "title": doc.title,
                                     "content": piece, "allowed_roles": roles, "source_uri": doc.object_key,
                                     "chunk_index": i, "section_path": section_path, "page": page},
                        ) for i, ((piece, section_path, page), v) in enumerate(zip(pieces, vectors))])
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
