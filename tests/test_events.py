import asyncio

from app import events


def test_outbox_events_use_tenant_and_document_id_as_kafka_key(monkeypatch):
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
    rows = [("11", "document.index", "101", {"tenant_id": "tenant", "document_id": "101"}, 1),
            ("12", "document.index", "102", {"tenant_id": "tenant", "document_id": "102"}, 1)]
    monkeypatch.setattr(events, "_claim_pending", lambda limit: rows)
    finished = []
    monkeypatch.setattr(events, "_finish_outbox", lambda row_id: finished.append(row_id))
    assert asyncio.run(events.publish_pending_outbox()) == 2
    assert [item[2] for item in producer.sent] == [b"tenant:101", b"tenant:102"]
    assert finished == ["11", "12"]
