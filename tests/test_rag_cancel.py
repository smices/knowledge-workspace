import asyncio

from app import main as rag
from app.auth import Principal


def test_answer_cancels_work_when_client_disconnects(monkeypatch):
    cancelled = False

    async def slow_answer(body, principal):
        nonlocal cancelled
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled = True
            raise

    class DisconnectedRequest:
        async def is_disconnected(self):
            return True

    async def run():
        monkeypatch.setattr(rag, '_answer', slow_answer)
        return await rag.answer(
            rag.AnswerRequest(query='cancel me'),
            DisconnectedRequest(),
            Principal(subject='test', tenant_id='tenant', roles=frozenset({'reader'})),
        )

    response = asyncio.run(run())
    assert response.status_code == 499
    assert cancelled
