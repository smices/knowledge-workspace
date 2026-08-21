import re
from hashlib import sha256
from types import SimpleNamespace
from opencc import OpenCC
from qdrant_client import AsyncQdrantClient, QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse
from app.config import settings

client = QdrantClient(url=settings.qdrant_url)
async_client = AsyncQdrantClient(url=settings.qdrant_url)
_to_traditional = OpenCC("s2t").convert
MIN_LEXICAL_SCORE = 0.2
SPARSE_VECTOR_NAME = "lexical"
SPARSE_HASH_SIZE = 1 << 20


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
        if SPARSE_VECTOR_NAME not in (info.config.params.sparse_vectors or {}):
            raise RuntimeError("Qdrant collection has no lexical sparse vector; use a new QDRANT_COLLECTION and reindex")
        return
    else:
        try:
            client.create_collection(settings.qdrant_collection,
                vectors_config=models.VectorParams(size=settings.embedding_dimensions, distance=models.Distance.COSINE),
                sparse_vectors_config={SPARSE_VECTOR_NAME: models.SparseVectorParams(modifier=models.Modifier.IDF)})
        except UnexpectedResponse as exc:
            if exc.status_code != 409 or "already exists" not in exc.content.decode(errors="replace").lower():
                raise
            return
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


def sparse_vector(text: str) -> models.SparseVector:
    """Stable lightweight lexical representation for Qdrant sparse retrieval."""
    terms = set(re.findall(r"[A-Za-z0-9_]{2,}", _to_traditional(text).lower()))
    for block in re.findall(r"[\u4e00-\u9fff]+", _to_traditional(text)):
        terms.update(block[i:i + 2] for i in range(len(block) - 1))
    indices = sorted({int.from_bytes(sha256(term.encode()).digest()[:4], "big") % SPARSE_HASH_SIZE for term in terms})
    return models.SparseVector(indices=indices, values=[1.0] * len(indices))


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
    accepted = [point for point in points
                if _lexical_score(query, (point.payload or {}).get("content", "")) >= MIN_LEXICAL_SCORE
                and _has_focus_parts(query, (point.payload or {}).get("content", ""))]
    return [SimpleNamespace(payload=point.payload, score=point.score) for point in accepted[:limit]]


def _filter(tenant_id: str, roles: set[str]) -> models.Filter:
    return models.Filter(must=[
        models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id)),
        models.FieldCondition(key="allowed_roles", match=models.MatchAny(any=list(roles))),
    ])


def _prefetch(vector, query_text: str, filters: models.Filter, limit: int):
    return [
        models.Prefetch(query=vector, filter=filters, limit=limit),
        models.Prefetch(query=sparse_vector(query_text), using=SPARSE_VECTOR_NAME, filter=filters, limit=limit),
    ]


def search(vector, tenant_id: str, roles: set[str], limit: int, query_text: str | None = None):
    filters = _filter(tenant_id, roles)
    if not query_text:
        return client.query_points(collection_name=settings.qdrant_collection, query=vector,
                                   query_filter=filters, limit=limit, with_payload=True).points
    candidate_limit = min(max(limit * 6, 24), 120)
    points = client.query_points(collection_name=settings.qdrant_collection,
        prefetch=_prefetch(vector, query_text, filters, candidate_limit),
        query=models.FusionQuery(fusion=models.Fusion.RRF), limit=candidate_limit, with_payload=True).points
    return _rank_points(points, query_text, limit)


async def search_async(vector, tenant_id: str, roles: set[str], limit: int,
                       query_text: str | None = None):
    """Cancellable, tenant- and role-filtered hybrid retrieval."""
    filters = _filter(tenant_id, roles)
    if not query_text:
        return (await async_client.query_points(collection_name=settings.qdrant_collection, query=vector,
                                                query_filter=filters, limit=limit, with_payload=True)).points
    candidate_limit = min(max(limit * 6, 24), 120)
    points = (await async_client.query_points(collection_name=settings.qdrant_collection,
        prefetch=_prefetch(vector, query_text, filters, candidate_limit),
        query=models.FusionQuery(fusion=models.Fusion.RRF), limit=candidate_limit, with_payload=True)).points
    return _rank_points(points, query_text, limit)
