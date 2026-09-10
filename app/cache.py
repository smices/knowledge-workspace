import asyncio
import json
import re
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from math import fsum, sqrt
from typing import Awaitable, Callable

from redis import Redis
from sqlalchemy import select, update

from app.config import settings


cache = Redis.from_url(settings.redis_url, decode_responses=True,
                       socket_timeout=settings.dependency_timeout_seconds,
                       socket_connect_timeout=settings.dependency_timeout_seconds)


def cache_key(kind: str, *parts: object) -> str:
    return f"knowledge:{kind}:" + sha256("|".join(map(str, parts)).encode()).hexdigest()


def cache_get(key: str):
    try:
        value = cache.get(key)
        return json.loads(value) if value else None
    except Exception:
        return None


def cache_put(key: str, value: object, ttl: int) -> None:
    try:
        cache.setex(key, ttl, json.dumps(value, ensure_ascii=False))
    except Exception:
        pass


def normalize_query(query: str) -> str:
    value = unicodedata.normalize("NFKC", query).strip().casefold()
    value = re.sub(r"[\s\u3000]+", " ", value)
    return re.sub(r"[?？!！。,.，;；:：]+$", "", value).strip()


def knowledge_revision(tenant_id: str, db=None) -> int | None:
    """Read the PostgreSQL-owned revision; cache failures fail closed."""
    try:
        if db is None:
            from app.db import SessionLocal
            with SessionLocal() as session:
                value = session.scalar(select(_tenant_model().knowledge_revision).where(_tenant_model().id == tenant_id))
        else:
            value = db.scalar(select(_tenant_model().knowledge_revision).where(_tenant_model().id == tenant_id))
        return int(value) if value is not None else None
    except Exception:
        return None


def bump_knowledge_revision(tenant_id: str, db=None) -> int | None:
    """Atomically bump a tenant revision; a passed transaction owns commit."""
    if db is not None:
        return _bump(db, tenant_id)
    try:
        from app.db import SessionLocal
        with SessionLocal() as session:
            value = _bump(session, tenant_id)
            session.commit()
            return value
    except Exception:
        return None


def _tenant_model():
    from app.db import Tenant
    return Tenant


def _bump(db, tenant_id: str) -> int | None:
    tenant = _tenant_model()
    changed = db.execute(
        update(tenant)
        .where(tenant.id == tenant_id)
        .values(knowledge_revision=tenant.knowledge_revision + 1)
    )
    if not changed.rowcount:
        return None
    db.flush()
    return db.scalar(select(tenant.knowledge_revision).where(tenant.id == tenant_id))


ENTITY_NOISE = re.compile(
    r"^(请问|请|帮我|告诉我|分析|比较|说明|解释|查询|查找|一下|有关|关于|的|是|有|什么|"
    r"为何|为什么|如何|怎样|哪些|多少|关系|区别|差异|联系|内容|情况)+|"
    r"(是什么关系|是什么|有什么|的关系|有何关系|之间|如何|怎样|关系|吗|呢|么)+$"
)
GENERIC_ENTITIES = {"依赖", "关联", "联系", "区别", "差异", "比较", "标准", "规定", "流程"}
SEMANTIC_SIMILARITY_THRESHOLD = 0.70


def query_entities(query: str) -> tuple[str, ...]:
    value = normalize_query(query)
    quoted = re.findall(r"[\"'“‘《]([^\"'”’》]{2,64})[\"'”’》]", value)
    parts = re.split(r"(?:和|与|及|以及|、|/|vs\.?|versus)|[\s，,。！？?!；;：:()（）]+", value)
    entities = []
    for part in quoted + parts:
        part = ENTITY_NOISE.sub("", part).strip(" -—_·")
        if 2 <= len(part) <= 64 and part not in GENERIC_ENTITIES and part not in entities:
            entities.append(part)
    return tuple(sorted(entities))


def evidence_signature(contexts: list[dict]) -> tuple[str, ...]:
    return tuple(sorted(
        f"{item.get('document_id', '')}:{item.get('document_version', 1)}:{item.get('chunk_index', '')}"
        for item in contexts
    ))


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    norm = sqrt(fsum(x * x for x in left) * fsum(x * x for x in right))
    return fsum(x * y for x, y in zip(left, right)) / norm if norm else 0.0


def semantic_get(bucket: str, vector: list[float], entities: tuple[str, ...],
                 evidence: tuple[str, ...], threshold: float = SEMANTIC_SIMILARITY_THRESHOLD):
    try:
        entries = cache.lrange(bucket, 0, 39)
    except Exception:
        return None
    for raw in entries:
        try:
            item = json.loads(raw)
            if (tuple(item["entities"]) == entities and tuple(item["evidence"]) == evidence
                    and _cosine(vector, item["vector"]) >= threshold):
                return item["result"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return None


def semantic_put(bucket: str, vector: list[float], entities: tuple[str, ...],
                 evidence: tuple[str, ...], result: dict, ttl: int = 86400) -> None:
    try:
        payload = json.dumps({"vector": vector, "entities": entities, "evidence": evidence,
                              "result": result}, ensure_ascii=False)
        cache.lpush(bucket, payload)
        cache.ltrim(bucket, 0, 39)
        cache.expire(bucket, ttl)
    except Exception:
        pass


@dataclass
class _Flight:
    task: asyncio.Task
    waiters: int = 0


_flights: dict[str, _Flight] = {}
_flight_lock = asyncio.Lock()


async def singleflight(key: str, factory: Callable[[], Awaitable[dict]]) -> tuple[dict, bool]:
    async with _flight_lock:
        flight = _flights.get(key)
        joined = flight is not None
        if flight is None:
            flight = _Flight(asyncio.create_task(factory()))
            _flights[key] = flight
        flight.waiters += 1
    try:
        return await asyncio.shield(flight.task), joined
    finally:
        cleanup = None
        async with _flight_lock:
            flight.waiters -= 1
            if flight.waiters == 0:
                if not flight.task.done():
                    flight.task.cancel()
                    cleanup = flight.task
                _flights.pop(key, None)
        # The final waiter retains its admission lease until shared work stops.
        if cleanup is not None:
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not cleanup.cancelled():
                cleanup.exception()
