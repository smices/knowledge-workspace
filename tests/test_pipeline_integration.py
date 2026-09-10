"""Opt-in real PostgreSQL/S3/Kafka/Qdrant path with deterministic embeddings."""
import asyncio
from io import BytesIO
import json
import os
from uuid import uuid4

import boto3
from botocore.config import Config
from fastapi import UploadFile
import pytest
from qdrant_client import QdrantClient, models
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app import cache, db, events, main, storage, vector
from app.auth import Principal
from app.config import settings
from worker import main as worker

REQUIRED = ("TEST_DATABASE_URL", "TEST_QDRANT_URL", "TEST_S3_URL", "TEST_S3_ACCESS_KEY",
            "TEST_S3_SECRET_KEY", "TEST_KAFKA_BOOTSTRAP")
pytestmark = pytest.mark.skipif(not all(os.getenv(key) for key in REQUIRED),
                                reason="requires isolated pipeline test services")


@pytest.fixture
def pipeline(monkeypatch):
    suffix = uuid4().hex
    schema, collection, bucket = "pipeline_" + suffix, "pipeline_" + suffix, "pipeline-" + suffix
    url = make_url(os.environ["TEST_DATABASE_URL"])
    control = create_engine(url)
    with control.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url.update_query_dict({"options": f"-csearch_path={schema}"}))
    client = QdrantClient(url=os.environ["TEST_QDRANT_URL"], timeout=5)
    s3 = boto3.client("s3", endpoint_url=os.environ["TEST_S3_URL"],
                      aws_access_key_id=os.environ["TEST_S3_ACCESS_KEY"],
                      aws_secret_access_key=os.environ["TEST_S3_SECRET_KEY"], region_name="us-east-1",
                      config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=5))
    try:
        db.Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine, expire_on_commit=False)
        for module in (db, main, worker, events, vector):
            monkeypatch.setattr(module, "SessionLocal", session)
        monkeypatch.setattr(settings, "embedding_dimensions", 3)
        monkeypatch.setattr(settings, "qdrant_collection", collection)
        monkeypatch.setattr(settings, "s3_bucket", bucket)
        monkeypatch.setattr(settings, "kafka_bootstrap_servers", os.environ["TEST_KAFKA_BOOTSTRAP"])
        monkeypatch.setattr(settings, "kafka_document_topic", "pipeline-" + suffix)
        monkeypatch.setattr(storage, "s3", s3)
        monkeypatch.setattr(vector, "client", client)
        monkeypatch.setattr(worker, "client", client)
        monkeypatch.setattr(worker, "embed", lambda texts: [[1.0, 0.0, 0.0] for _ in texts])
        client.create_collection(collection, vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE),
                                 sparse_vectors_config={"lexical": models.SparseVectorParams()})
        yield session, client
    finally:
        client.delete_collection(collection)
        client.close()
        # Only this fixture's unique bucket and schema are removed.
        if bucket in [row["Name"] for row in s3.list_buckets()["Buckets"]]:
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
                for item in page.get("Contents", []):
                    s3.delete_object(Bucket=bucket, Key=item["Key"])
            s3.delete_bucket(Bucket=bucket)
        engine.dispose()
        with control.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        control.dispose()


def test_real_upload_outbox_delivery_index_authorization_and_delete(pipeline):
    from aiokafka import AIOKafkaConsumer

    session, client = pipeline
    principal = Principal("integration-user", "integration-tenant", frozenset({"admin", "hr"}))

    async def run():
        accepted = await main.upload_document(
            UploadFile(filename="synthetic.txt", file=BytesIO(b"Synthetic HR policy for integration testing.")),
            "hr", None, principal)
        with session() as store:
            pending = store.query(db.EventOutbox).one()
            assert pending.status == "pending"
            assert store.query(db.IngestionJob).count() == 1
        consumer = AIOKafkaConsumer(settings.kafka_document_topic,
                                    bootstrap_servers=settings.kafka_bootstrap_servers,
                                    group_id="integration-" + uuid4().hex,
                                    auto_offset_reset="earliest", enable_auto_commit=False)
        await consumer.start()
        try:
            assert await events.publish_pending_outbox() == 1
            message = await asyncio.wait_for(consumer.getone(), timeout=20)
            event = json.loads(message.value)
            assert message.key == b"integration-tenant:" + accepted["document_id"].encode()
            assert await asyncio.to_thread(worker.handle_event, event) == "success"
            assert await asyncio.to_thread(worker.handle_event, event) == "obsolete"
            await consumer.commit()
        finally:
            await consumer.stop()
        return accepted

    accepted = asyncio.run(run())
    found = vector.search([1.0, 0.0, 0.0], principal.tenant_id, {"hr"}, 5)
    assert len(found) == 1
    assert found[0].payload["document_id"] == accepted["document_id"]
    assert vector.search([1.0, 0.0, 0.0], principal.tenant_id, {"finance"}, 5) == []
    assert vector.search([1.0, 0.0, 0.0], "other-tenant", {"hr"}, 5) == []
    before = cache.knowledge_revision(principal.tenant_id)
    main.delete_document(accepted["document_id"], principal)
    assert cache.knowledge_revision(principal.tenant_id) > before
    # Tombstone hides still-present points before asynchronous physical cleanup.
    assert vector.search([1.0, 0.0, 0.0], principal.tenant_id, {"hr"}, 5) == []
    with session() as store:
        cleanup = store.query(db.EventOutbox).filter_by(event_type="document.delete").one().payload
    assert worker.handle_event(cleanup) == "success"
    assert client.count(settings.qdrant_collection, exact=True).count == 0
    malformed = dict(cleanup, tenant_id="invalid\u0000tenant", trace_id="invalid\ud800trace")
    assert worker.handle_message(json.dumps(malformed).encode()) == "dlq"
    with session() as store:
        dead_letter = store.query(db.IngestionDeadLetter).one()
        assert dead_letter.error_code == "malformed_event"
        assert "\x00" not in (dead_letter.tenant_id or "")
        (dead_letter.trace_id or "").encode("utf-8")
