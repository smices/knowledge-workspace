import asyncio
from contextlib import asynccontextmanager
import pytest

from app import main as rag
from app.auth import Principal


@pytest.mark.parametrize("endpoint,work_name", [("answer", "_answer"), ("retrieve", "_retrieve"), ("graph", "_graph")])
def test_answer_cancels_work_when_client_disconnects(monkeypatch, endpoint, work_name):
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
        @asynccontextmanager
        async def admitted(_):
            yield

        monkeypatch.setattr(rag, 'admit', admitted)
        monkeypatch.setattr(rag, work_name, slow_answer)
        return await getattr(rag, endpoint)(
            rag.AnswerRequest(query='cancel me'),
            DisconnectedRequest(),
            Principal(subject='test', tenant_id='tenant', roles=frozenset({'reader'})),
        )

    response = asyncio.run(run())
    assert response.status_code == 499
    assert cancelled
