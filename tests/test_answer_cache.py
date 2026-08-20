import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException
from app import cache as answer_cache
from app import main as rag
from app.auth import Principal


def test_query_normalization_and_entity_scope():
    assert answer_cache.normalize_query("  刘备和诸葛亮是什么关系？ ") == "刘备和诸葛亮是什么关系"
    assert answer_cache.query_entities("刘备和诸葛亮是什么关系？") == ("刘备", "诸葛亮")
    assert answer_cache.query_entities("诸葛亮与刘备的关系") == ("刘备", "诸葛亮")


def test_semantic_cache_requires_entities_evidence_and_similarity(monkeypatch):
    entry = {
        "vector": [1.0, 0.0],
        "entities": ["刘备", "诸葛亮"],
        "evidence": ["doc:1:2"],
        "result": {"answer_id": "cached"},
    }

    class FakeRedis:
        def lrange(self, *_):
            import json
            return [json.dumps(entry, ensure_ascii=False)]

    monkeypatch.setattr(answer_cache, "cache", FakeRedis())
    assert answer_cache.semantic_get("bucket", [0.99, 0.01], ("刘备", "诸葛亮"),
                                     ("doc:1:2",)) == {"answer_id": "cached"}
    assert answer_cache.semantic_get("bucket", [0.99, 0.01], ("刘备",), ("doc:1:2",)) is None
    assert answer_cache.semantic_get("bucket", [0.99, 0.01], ("刘备", "诸葛亮"),
                                     ("doc:1:3",)) is None


def test_singleflight_merges_same_inflight_answer():
    calls = 0

    async def run():
        nonlocal calls

        async def work():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return {"answer": "ok"}

        return await asyncio.gather(
            answer_cache.singleflight("same", work),
            answer_cache.singleflight("same", work),
        )

    results = asyncio.run(run())
    assert calls == 1
    assert results == [({"answer": "ok"}, False), ({"answer": "ok"}, True)]


def test_feedback_token_is_bound_to_answer_and_actor():
    answer_id = str(uuid4())
    alice = Principal(subject="alice", tenant_id="tenant", roles=frozenset({"reader"}))
    bob = Principal(subject="bob", tenant_id="tenant", roles=frozenset({"reader"}))
    token = rag._feedback_token(answer_id, alice)
    assert len(token) == 64
    assert token != rag._feedback_token(answer_id, bob)
    with pytest.raises(HTTPException) as error:
        rag.set_answer_feedback(answer_id, rag.FeedbackRequest(liked=True, token="0" * 64), alice)
    assert error.value.status_code == 403
