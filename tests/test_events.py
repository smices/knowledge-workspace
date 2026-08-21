import asyncio

from app import events


def test_document_events_use_document_id_as_kafka_key(monkeypatch):
    class Producer:
        sent = []

        async def start(self):
            pass

        async def stop(self):
            pass

        async def send_and_wait(self, topic, value, key):
            self.sent.append((topic, value, key))

    producer = Producer()
    monkeypatch.setattr(events, "AIOKafkaProducer", lambda **kwargs: producer)
    asyncio.run(events.publish_documents(["document-a", "document-b"]))
    assert [item[2] for item in producer.sent] == [b"document-a", b"document-b"]
