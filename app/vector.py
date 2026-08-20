import re
from types import SimpleNamespace
from opencc import OpenCC
from qdrant_client import AsyncQdrantClient, QdrantClient, models
from app.config import settings

client = QdrantClient(url=settings.qdrant_url)
async_client = AsyncQdrantClient(url=settings.qdrant_url)
_to_traditional = OpenCC("s2t").convert
MIN_LEXICAL_SCORE = 0.2
MIN_SEMANTIC_SCORE = 0.28


def ensure_collection():
    names = {c.name for c in client.get_collections().collections}
    if settings.qdrant_collection in names:
        info = client.get_collection(settings.qdrant_collection)
        actual = info.config.params.vectors.size
        if actual != settings.embedding_dimensions:
            raise RuntimeError(
                f"Qdrant collection dimension {actual} != configured {settings.embedding_dimensions}; "
                "rebuild the local collection before changing embedding models"
            )
    else:
        client.create_collection(settings.qdrant_collection,
            vectors_config=models.VectorParams(size=settings.embedding_dimensions, distance=models.Distance.COSINE))
    for field in ("tenant_id", "document_id", "allowed_roles"):
        try:
            client.create_payload_index(settings.qdrant_collection, field, models.PayloadSchemaType.KEYWORD)
        except Exception:
            pass
    try:
        client.create_payload_index(settings.qdrant_collection, "content", models.TextIndexParams(type=models.TextIndexType.TEXT, tokenizer=models.TokenizerType.MULTILINGUAL, min_token_len=1, max_token_len=40, lowercase=True))
    except Exception:
        pass


def _focus_parts(text: str) -> list[str]:
    focus = _strip_question(text)
    return [part for block in re.findall(r"[\u4e00-\u9fff]+", focus) for part in re.split(r"[和與及跟、的]", block) if len(part) >= 2]


def _strip_question(text: str) -> str:
    return re.sub(r"(是什麼關係|(?:之間)?(?:的)?關係|是誰|為什麼|為何|如何|有哪些|多少|嗎|呢).*", "", _to_traditional(text))


def _terms(text: str) -> set[str]:
    focus = _strip_question(text)
    terms = set(re.findall(r"[A-Za-z0-9_]{2,}", focus.lower()))
    for block in re.findall(r"[\u4e00-\u9fff]+", focus):
        for part in re.split(r"[和與及跟、的]", block):
            if len(part) >= 2:
                terms.add(part)
                terms.update(part[i:i + 2] for i in range(len(part) - 1))
    return terms


def _lexical_score(query: str, content: str) -> float:
    terms = _terms(query)
    if not terms:
        return 0.0
    haystack = _to_traditional(content).lower()
    return sum(term in haystack for term in terms) / len(terms)


def _has_focus_parts(query: str, content: str) -> bool:
    haystack = _to_traditional(content)
    parts = _focus_parts(query)
    return len(parts) < 2 or all(part in haystack for part in parts)


def _rank_points(points, query: str, limit: int):
    """Apply relevance and co-occurrence gates before returning evidence."""
    scored = [(point, _lexical_score(query, (point.payload or {}).get("content", ""))) for point in points]
    scored = [(point, score) for point, score in scored if score >= MIN_LEXICAL_SCORE and _has_focus_parts(query, (point.payload or {}).get("content", ""))]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [SimpleNamespace(payload=point.payload, score=score) for point, score in scored[:limit]]


def search(vector, tenant_id: str, roles: set[str], limit: int, query_text: str | None = None):
    candidate_limit = min(max(limit * 4, 20), 100) if query_text else limit
    points = client.query_points(
        collection_name=settings.qdrant_collection,
        query=vector,
        query_filter=models.Filter(must=[
            models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id)),
            models.FieldCondition(key="allowed_roles", match=models.MatchAny(any=list(roles))),
        ]),
        limit=candidate_limit,
        with_payload=True,
    ).points
    if query_text:
        filters = models.Filter(must=[
            models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id)),
            models.FieldCondition(key="allowed_roles", match=models.MatchAny(any=list(roles))),
        ])
        text_points, _ = client.scroll(
            collection_name=settings.qdrant_collection,
            scroll_filter=filters,
            limit=5000,
            with_payload=True,
        )
        lexical = _rank_points(text_points, query_text, limit)
        if lexical:
            return lexical
        if len(_focus_parts(query_text)) >= 2:
            return []
        semantic = [(point, _lexical_score(query_text, (point.payload or {}).get("content", ""))) for point in points
                    if point.score >= MIN_SEMANTIC_SCORE]
        semantic.sort(key=lambda pair: 0.4 * pair[0].score + 0.6 * pair[1], reverse=True)
        return [point for point, _ in semantic[:limit]]
    return points[:limit]


async def search_async(vector, tenant_id: str, roles: set[str], limit: int,
                       query_text: str | None = None):
    """Cancellable retrieval path for interactive requests."""
    query_filter = models.Filter(must=[
        models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id)),
        models.FieldCondition(key="allowed_roles", match=models.MatchAny(any=list(roles))),
    ])
    candidate_limit = min(max(limit * 4, 20), 100) if query_text else limit
    points = (await async_client.query_points(
        collection_name=settings.qdrant_collection,
        query=vector,
        query_filter=query_filter,
        limit=candidate_limit,
        with_payload=True,
    )).points
    if not query_text:
        return points[:limit]

    records, _ = await async_client.scroll(
        collection_name=settings.qdrant_collection,
        scroll_filter=query_filter,
        limit=5000,
        with_payload=True,
    )
    focus_parts = _focus_parts(query_text)
    lexical = _rank_points(records, query_text, limit)
    if lexical:
        return lexical
    if len(focus_parts) >= 2:
        return []
    semantic = [(point, _lexical_score(query_text, (point.payload or {}).get("content", "")))
                for point in points if point.score >= MIN_SEMANTIC_SCORE]
    semantic.sort(key=lambda pair: 0.4 * pair[0].score + 0.6 * pair[1], reverse=True)
    return [point for point, _ in semantic[:limit]]
