import json
from aiokafka import AIOKafkaProducer
from app.config import settings

async def publish_documents(document_ids: list[str]):
    producer = AIOKafkaProducer(bootstrap_servers=settings.kafka_bootstrap_servers)
    await producer.start()
    try:
        for document_id in document_ids:
            await producer.send_and_wait(settings.kafka_document_topic, json.dumps({"document_id": document_id}).encode(),
                                         key=document_id.encode())
    finally:
        await producer.stop()


async def publish_document(document_id: str):
    await publish_documents([document_id])
