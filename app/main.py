from pathlib import Path
from typing import Literal

import asyncio
import hmac
import json
import logging
import mimetypes
import re
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from time import perf_counter
from urllib.parse import urlparse
from uuid import UUID, uuid4

from fastapi import Depends, File, Form, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI, OpenAI
from qdrant_client import models
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.auth import (
    Principal,
    _sign_session,
    authorization_request,
    browser_session_active,
    exchange_code,
    identity_account_url,
    identity_logout_url,
    principal_from_session,
    safe_next_path,
)
from app.cache import (
    bump_knowledge_revision,
    cache,
    cache_get,
    cache_key,
    cache_put,
    evidence_signature,
    knowledge_revision,
    normalize_query,
    query_entities,
    semantic_get,
    semantic_put,
    singleflight,
)
from app.config import settings
from app.admission import admit, run_sync
from app.db import (
    AnswerFeedback,
    AuditEvent,
    Chunk,
    Document,
    DocumentGrant,
    DocumentVersion,
    EntityAlias,
    IngestionJob,
    KnowledgeBase,
    KnowledgeRelation,
    Principal as DbPrincipal,
    PrincipalRole,
    QueryEvent,
    Role,
    SessionLocal,
    Tenant,
    LocalAdminCredential,
    LocalUserCredential,
    engine,
    init_db,
)
from app.local_admin import (LOCAL_ADMIN_PASSWORD_MAX_LENGTH, authenticate_local,
                             authenticate_local_admin, ensure_local_admin, hash_password,
                             normalize_username, validate_password, verify_password)
from app.events import enqueue_document
from app.storage import MAX_UPLOAD_BYTES, delete_file, file_exists, put_file, read_upload
from app.vector import (VersionScopeOverflow, _focus_parts, async_client as async_vector_client, client as vector_client,
                        ensure_collection, normalize_mention, search, search_async)

llm = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url,
             timeout=settings.model_timeout_seconds, max_retries=1)
async_llm = AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url,
                       timeout=settings.model_timeout_seconds, max_retries=1)
CACHE_CONTRACT_VERSION = "direct-evidence-v5"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.auth_mode.lower() == "dev":
        init_db()
    if settings.auth_mode.lower() in {"local", "oidc"} and (settings.local_admin_username or settings.local_admin_password):
        ensure_local_admin()
    ensure_collection()
    try:
        yield
    finally:
        await async_llm.close()
        await async_vector_client.close()


app = FastAPI(title=f"{settings.brand_name} API", version="0.2.0", lifespan=lifespan)
logger = logging.getLogger("uvicorn.error")


class RequestMetrics:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        trace_id, started, status = str(uuid4()), perf_counter(), 500

        async def traced_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message = dict(message, headers=[*message.get("headers", []),
                                                 (b"x-request-id", trace_id.encode())])
            await send(message)

        try:
            await self.app(scope, receive, traced_send)
        finally:
            logger.info(json.dumps({"event": "knowledge_request", "request_id": trace_id,
                                    "method": scope["method"], "route": getattr(scope.get("route"), "path", "unmatched"),
                                    "status": status, "duration_ms": round((perf_counter() - started) * 1000, 1)}))


app.add_middleware(RequestMetrics)


@app.middleware("http")
async def browser_write_origin_gate(request: Request, call_next):
    """CSRF-protect every browser state change; machine Bearer calls stay API-safe."""
    if (request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and not request.headers.get("authorization", "").startswith("Bearer ")):
        try:
            require_same_origin(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return await call_next(request)


@app.middleware("http")
async def admin_page_gate(request: Request, call_next):
    path = request.url.path
    is_admin_html = path == "/admin" or path == "/admin/" or (
        path.startswith("/admin/") and not Path(path).suffix
    ) or "text/html" in request.headers.get("accept", "")
    if is_admin_html and (path == "/admin" or path.startswith("/admin/")):
        if not browser_session_active(request):
            # A stale or wrong-source cookie is an authenticated browser
            # attempt, not an anonymous visit.  Do not turn it into a login
            # redirect that could hide a local/IdP mode boundary failure.
            if request.cookies.get(settings.identity_session_cookie):
                return Response("admin role required", status_code=403)
            return RedirectResponse("/login", status_code=303)
        try:
            principal = principal_from_session(request, None)
            require_admin(principal)
        except HTTPException as exc:
            if exc.status_code in {401, 403}:
                return Response("admin role required", status_code=403)
            raise
    return await call_next(request)


@app.exception_handler(VersionScopeOverflow)
async def version_scope_overflow(_request, _error):
    return JSONResponse(status_code=503, content={"detail": "Document scope exceeds retrieval capacity"})


@app.exception_handler(RequestValidationError)
async def safe_validation_error(_request, error: RequestValidationError):
    # Never serialize ``input``/``ctx``: authentication validation inputs may
    # contain a password or another credential supplied in the request body.
    detail = [{"loc": item.get("loc", ()), "type": item.get("type", "value_error"),
               "msg": item.get("msg", "invalid value")} for item in error.errors()]
    return JSONResponse(status_code=422, content={"detail": detail})


@app.get("/brand.js", include_in_schema=False)
def brand_script():
    config = {
        "name": settings.brand_name,
        "mark": settings.brand_mark,
        "tagline": settings.brand_tagline,
        "footer": settings.brand_footer,
        "primaryColor": settings.brand_primary_color,
        "logoUrl": settings.brand_logo_url,
    }
    body = "window.__APP_BRAND__=" + json.dumps(config, ensure_ascii=False).replace("</", "<\\/") + ";"
    return Response(body, media_type="application/javascript", headers={"Cache-Control": "no-store"})


app.mount("/assets", StaticFiles(directory="web"), name="assets")


class SpaStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or Path(path).suffix:
                raise
            return await super().get_response("index.html", scope)


if Path("admin/dist").exists():
    app.mount("/admin", SpaStaticFiles(directory="admin/dist", html=True), name="admin")


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=10000)
    limit: int = Field(default=8, ge=1, le=50)


class AnswerRequest(SearchRequest):
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)


class GraphRequest(SearchRequest):
    pass


class FeedbackRequest(BaseModel):
    liked: bool
    token: str = Field(min_length=64, max_length=64)


class RoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9:_-]+$")


class MemberRolesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: set[str] = Field(default_factory=set)


class DocumentGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: set[str] = Field(default_factory=set)


class MemberStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["active", "disabled"]


class MemberCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["local", "idp"]
    username: str | None = None
    password: str | None = None
    subject: str | None = None
    roles: set[str] = Field(default_factory=set)

    @model_validator(mode="after")
    def validate_source_fields(self):
        if self.source == "local":
            if self.username is None or self.password is None or self.subject is not None:
                raise ValueError("local members require username and password only")
        elif self.subject is None or self.username is not None or self.password is not None:
            raise ValueError("idp members require subject and roles only")
        return self


class PasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_password: str


class AccountPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str
    new_password: str


class EntityAliasRequest(BaseModel):
    document_id: str = Field(min_length=36, max_length=36)
    chunk_index: int = Field(ge=0)
    canonical: str = Field(min_length=2, max_length=255)
    alias: str = Field(min_length=2, max_length=255)


class EntityAliasStatusRequest(BaseModel):
    status: Literal["approved", "rejected"]


def hmac_compare(left: str, right: str) -> bool:
    import hmac
    return bool(left) and hmac.compare_digest(left, right)


def require_same_origin(request: Request) -> None:
    """Require a browser write to carry a same-origin Origin or Referer."""
    values = [request.headers.get("origin"), request.headers.get("referer")]
    candidates = [value for value in values if value]
    if not candidates:
        raise HTTPException(403, "same-origin request required")
    request_origin = f"{request.url.scheme}://{request.url.netloc}".rstrip("/")
    configured = urlparse(settings.identity_redirect_uri)
    configured_origin = (f"{configured.scheme}://{configured.netloc}".rstrip("/")
                         if configured.scheme and configured.netloc else "")
    allowed = {configured_origin}
    # Test/local loopback hosts are intentionally allowed to use their request
    # origin; deployed origins remain pinned to the configured redirect URI.
    if request.url.hostname in {"localhost", "127.0.0.1", "::1", "testserver"}:
        allowed.add(request_origin)
    parsed = []
    for value in candidates:
        try:
            item = urlparse(value)
            if item.scheme not in {"http", "https"} or not item.netloc:
                raise ValueError
            if value == request.headers.get("origin") and (item.path not in {"", "/"}
                                                             or item.query or item.fragment):
                raise ValueError
            parsed.append(f"{item.scheme}://{item.netloc}".rstrip("/"))
        except ValueError as exc:
            raise HTTPException(403, "same-origin request required") from exc
    if any(origin not in allowed for origin in parsed) or len(set(parsed)) != 1:
        raise HTTPException(403, "same-origin request required")


def _local_login_attempt_allowed(request: Request, username: str) -> bool:
    client_ip = request.client.host if request.client else "unknown"
    try:
        normalized = normalize_username(username)
    except ValueError:
        normalized = username.strip().casefold()[:64]
    tenant_id = settings.identity_default_tenant_id or "tenant-local"
    ip_key = "auth:local-login:ip:" + sha256(client_ip.encode()).hexdigest()
    user_key = "auth:local-login:user:" + sha256(f"{tenant_id}|{normalized}".encode()).hexdigest()
    script = """
    local ip_count = redis.call('INCR', KEYS[1])
    local user_count = redis.call('INCR', KEYS[2])
    if ip_count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
    if user_count == 1 then redis.call('EXPIRE', KEYS[2], ARGV[1]) end
    return {ip_count, user_count}
    """
    try:
        counts = cache.eval(script, 2, ip_key, user_key, 60)
        count = max(int(counts[0]), int(counts[1]))
    except Exception as exc:
        raise HTTPException(503, "authentication rate limiter unavailable") from exc
    return count <= settings.local_login_attempts_per_minute


def ensure_principal(db, principal: Principal) -> list[Role]:
    tenant = db.get(Tenant, principal.tenant_id)
    if tenant is None:
        tenant = Tenant(id=principal.tenant_id, name=principal.tenant_id)
        db.add(tenant)
    identity = db.get(DbPrincipal, principal.subject)
    if identity is None:
        db.add(DbPrincipal(id=principal.subject, tenant_id=principal.tenant_id))
    roles = []
    for role_name in principal.roles:
        role = db.scalar(select(Role).where(Role.tenant_id == principal.tenant_id,
                                            Role.name == role_name))
        if role is None:
            role = Role(id=str(uuid4()), tenant_id=principal.tenant_id, name=role_name)
            db.add(role)
        roles.append(role)
    db.flush()
    return roles


def log_query(principal: Principal, query: str, endpoint: str, result_count: int, started: float):
    with SessionLocal() as db:
        ensure_principal(db, principal)
        db.add(QueryEvent(tenant_id=principal.tenant_id, subject=principal.subject, query=query,
                          endpoint=endpoint, result_count=result_count,
                          duration_ms=int((perf_counter() - started) * 1000)))
        db.commit()


def citations_for(points, contexts):
    return [{"document_id": item.get("document_id"), "document_version": item.get("document_version", 1),
             "chunk_index": item.get("chunk_index"), "title": item.get("title"),
             "source_uri": item.get("source_uri"), "score": point.score,
             "content": item.get("content", "")}
            for point, item in zip(points, contexts)]


def approved_aliases(principal: Principal, query: str) -> tuple[dict, list[dict]]:
    entities = set(_focus_parts(query))
    if not entities:
        return {}, []
    with SessionLocal() as db:
        rows = db.execute(select(EntityAlias, DocumentVersion.document_id, DocumentVersion.version).join(
            DocumentVersion, DocumentVersion.id == EntityAlias.document_version_id
        ).join(Document, Document.id == DocumentVersion.document_id).where(
            EntityAlias.tenant_id == principal.tenant_id, EntityAlias.status == "approved",
            *document_access(principal), Document.status == "ready",
            DocumentVersion.status == "ready", Document.version == DocumentVersion.version,
        ).limit(5001)).all()
    # ponytail: bound alias expansion; add a normalized canonical index before this ceiling.
    if len(rows) > 5000:
        raise HTTPException(503, "Alias scope exceeds configured retrieval capacity")
    records = [{"id": alias.id, "canonical": normalize_mention(alias.canonical),
                "alias": normalize_mention(alias.alias), "document_id": document_id,
                "document_version": version,
                "chunk_index": alias.chunk_index, "confidence": 0.92}
               for alias, document_id, version in rows if normalize_mention(alias.canonical) in entities]
    mapping = {}
    for item in records:
        mapping.setdefault(item["canonical"], []).append({key: item[key] for key in ("alias", "document_id", "document_version")})
    return mapping, records


def alias_bindings(contexts: list[dict], records: list[dict]) -> list[dict]:
    bindings = []
    for index, context in enumerate(contexts, 1):
        content = normalize_mention(str(context.get("content", "")))
        for record in records:
            if (str(record["document_id"]) == str(context.get("document_id"))
                    and str(record["document_version"]) == str(context.get("document_version"))
                    and record["alias"] in content):
                binding = {"entity": record["canonical"], "matched_mention": record["alias"],
                           "alias_id": record["id"], "status": "approved",
                           "confidence": record["confidence"], "evidence": [index]}
                if binding not in bindings:
                    bindings.append(binding)
    return bindings


ANSWER_STATE_LABELS = {
    "answered": "已回答",
    "partial": "部分回答",
    "no_answer": "无答案",
    "conflict": "证据冲突",
}
EVIDENCE_MARKER = re.compile(r"\[证据\s*(\d+)\]")


def _evidence_ids(text: str, count: int) -> list[int]:
    return sorted({int(match) for match in EVIDENCE_MARKER.findall(text) if 1 <= int(match) <= count})


def _conclusion_text(answer: str) -> str:
    match = re.search(r"##\s*结论\s*(.*?)(?=\n##|\Z)", answer, flags=re.S)
    return match.group(1).strip() if match else ""


def _conclusion_claims(answer: str) -> list[str]:
    conclusion = _conclusion_text(answer)
    claims = [re.sub(r"^[-*•]\s*", "", line).strip() for line in conclusion.splitlines() if line.strip()]
    return claims or ([conclusion] if conclusion else [])


def direct_pair_support(query: str, claims: list[str], contexts: list[dict], records: list[dict]) -> list[bool | None]:
    """Require queried relationship pairs to co-occur in one source clause."""
    entities = [entity for entity in _focus_parts(query)
                if entity not in {"師父", "徒弟", "主公", "盟友", "對手", "關係", "人物", "團隊"}]
    result = []
    for claim in claims:
        named = [entity for entity in entities if normalize_mention(entity) in normalize_mention(claim)]
        if len(named) < 2:
            result.append(None)
            continue
        supported = False
        evidence_ids = _evidence_ids(claim, len(contexts)) or list(range(1, len(contexts) + 1))
        for index in evidence_ids:
            context = contexts[index - 1]
            clauses = re.split(r"[。！？；;，,：:]", normalize_mention(str(context.get("content", ""))))
            mentions = []
            for entity in named:
                values = [normalize_mention(entity)] + [record["alias"] for record in records
                          if record["canonical"] == normalize_mention(entity)
                          and str(record["document_id"]) == str(context.get("document_id"))
                          and str(record["document_version"]) == str(context.get("document_version"))]
                mentions.append(values)
            if any(all(any(value in clause for value in values) for values in mentions) for clause in clauses):
                supported = True
                break
        result.append(supported)
    return result


def answer_state(answer: str, contexts: list[dict]) -> str:
    """Classify answer completeness without treating retrieval alone as proof."""
    if not contexts:
        return "no_answer"
    if any(marker in answer for marker in ("证据冲突", "证据矛盾", "资料相互矛盾")):
        return "conflict"
    conclusion = _conclusion_text(answer)
    if not conclusion or "未生成可验证" in conclusion:
        return "no_answer"
    if "资料不足" in conclusion or "无法确认" in conclusion:
        return "partial" if len(conclusion.replace("资料不足", "").replace("无法确认", "").strip(" -：:") ) >= 4 else "no_answer"
    return "answered" if _evidence_ids(answer, len(contexts)) else "no_answer"


def evidence_contract(answer: str, contexts: list[dict], state: str | None = None,
                      relationship_support: list[str | None] | None = None) -> list[dict]:
    state = state or answer_state(answer, contexts)
    claims = _conclusion_claims(answer)
    all_ids = _evidence_ids(answer, len(contexts))
    result = []
    for index, claim in enumerate(claims):
        direct_ids = _evidence_ids(claim, len(contexts))
        ids = direct_ids or all_ids
        verified = relationship_support[index] if relationship_support and index < len(relationship_support) else None
        if verified == "indirect":
            support, confidence = "indirect", 0.1
        elif verified == "insufficient":
            support, confidence = "insufficient", 0.15
        elif state == "conflict":
            support, confidence = "conflict", 0.25
        elif not ids or state == "no_answer":
            support, confidence = "insufficient", 0.15
        elif state == "partial":
            support, confidence = "partial", 0.45
        elif direct_ids:
            support, confidence = "supported", 0.92
        else:
            support, confidence = "cited", 0.6
        result.append({"claim": claim, "evidence": ids, "support": support, "confidence": confidence})
    return result


def direct_only_answer(contract: list[dict]) -> tuple[str, str]:
    direct = [item for item in contract if item["support"] == "supported"]
    if not direct:
        return "## 结论\n资料不足：原文未直接说明所问实体之间的关系。\n\n## 归纳\n不能以间接线索推导关系。\n\n## 依据", "no_answer"
    conclusion = "\n".join(f"- {item['claim']}" for item in direct)
    evidence = "\n".join(f"[证据 {index}]" for item in direct for index in item["evidence"])
    return f"## 结论\n{conclusion}\n\n## 归纳\n仅保留原文直接支持的关系；其余关系资料不足。\n\n## 依据\n{evidence}", "partial"


def graph_is_available(answer: str, contexts: list[dict]) -> bool:
    """Expose the graph action only when the answer has confirmed evidence."""
    return answer_state(answer, contexts) == "answered"


RELATION_ALIASES = {
    "主臣": "主从",
    "主仆": "主从",
    "部下": "主从",
    "从属": "主从",
    "辅佐": "辅佐",
    "军师": "辅佐",
    "跟随": "追随",
    "被命令": "追随",
    "师徒关系": "师徒",
    "师生": "师徒",
    "兄弟": "结义",
    "结拜": "结义",
    "亲密友爱": "亲密关系",
    "情感关系": "亲密关系",
    "朋友": "友好",
    "同僚": "合作",
    "联手": "合作",
    "交战": "敌对",
    "打斗": "敌对",
    "对手": "敌对",
}


def normalize_relation(label: str) -> str:
    """Canonicalize known aliases while retaining domain-specific labels."""
    label = " ".join(label.split())
    return RELATION_ALIASES.get(label, label[:48])


STANDARD_RELATIONS = frozenset(RELATION_ALIASES.values())


def parse_relationships(raw: str, contexts: list[dict], query: str | None = None, aliases=None) -> list[dict]:
    """Keep only direct, source-bound relations emitted by the model."""
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end < start:
        return []
    try:
        items = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []
    edges, seen = [], set()
    for item in items[:8]:
        if not isinstance(item, dict):
            continue
        source, target, label = (str(item.get(key, "")).strip() for key in ("source", "target", "relation"))
        label = normalize_relation(label)
        evidence = item.get("evidence")
        if not isinstance(evidence, int) or not (1 <= evidence <= len(contexts)):
            continue
        content = str(contexts[evidence - 1].get("content", ""))
        key = (source, target, label, evidence)
        def mentions(entity: str) -> list[str]:
            values = [entity]
            for item in (aliases or {}).get(normalize_mention(entity), []):
                if isinstance(item, str) or (str(item.get("document_id")) == str(contexts[evidence - 1].get("document_id"))
                                            and str(item.get("document_version")) == str(contexts[evidence - 1].get("document_version"))):
                    values.append(item if isinstance(item, str) else item["alias"])
            return values
        source_mentions, target_mentions = mentions(source), mentions(target)
        if (not source or not target or source == target or len(source) > 32 or len(target) > 32
                or not label or len(label) > 24
                or not any(normalize_mention(value) in normalize_mention(content) for value in source_mentions)
                or not any(normalize_mention(value) in normalize_mention(content) for value in target_mentions)
                or (query is not None and (source not in query or target not in query)) or key in seen):
            continue
        seen.add(key)
        edges.append({"source": source, "target": target, "label": label,
                      "relation_type": "standard" if label in STANDARD_RELATIONS else "custom",
                      "evidence": evidence, "excerpt": content[:260]})
    return edges


def merge_relationships(edges: list[dict]) -> list[dict]:
    merged = {}
    for edge in edges:
        label = normalize_relation(edge["label"])
        key = (edge["source"], edge["target"], label)
        item = merged.setdefault(key, {"source": edge["source"], "target": edge["target"],
                                       "label": label,
                                       "relation_type": "standard" if label in STANDARD_RELATIONS else "custom",
                                       "evidence": [], "excerpts": []})
        if edge["evidence"] not in item["evidence"]:
            item["evidence"].append(edge["evidence"])
        if edge.get("excerpt") and edge["excerpt"] not in item["excerpts"]:
            item["excerpts"].append(edge["excerpt"])
    return list(merged.values())


def persist_relationships(principal: Principal, contexts: list[dict], edges: list[dict]) -> None:
    """Keep evidence-backed relation candidates without making Qdrant authoritative."""
    document_keys = {(item.get("document_id"), item.get("document_version")) for item in contexts}
    with SessionLocal() as db:
        versions = db.execute(select(Document.id, DocumentVersion.version, DocumentVersion.id).join(
            DocumentVersion, DocumentVersion.document_id == Document.id
        ).where(*document_access(principal), Document.status == "ready",
                DocumentVersion.version == Document.version, DocumentVersion.status == "ready",
                Document.id.in_([key[0] for key in document_keys]))
            .order_by(Document.id).with_for_update(of=Document)).all()
        version_ids = {(document_id, version): version_id for document_id, version, version_id in versions
                       if (document_id, version) in document_keys}
        for edge in edges:
            item = contexts[edge["evidence"] - 1]
            version_id = version_ids.get((item.get("document_id"), item.get("document_version")))
            if version_id is None:
                continue
            exists = db.scalar(select(KnowledgeRelation.id).where(
                KnowledgeRelation.document_version_id == version_id,
                KnowledgeRelation.chunk_index == item.get("chunk_index"),
                KnowledgeRelation.source == edge["source"], KnowledgeRelation.target == edge["target"],
                KnowledgeRelation.relation == edge["label"],
            ))
            if exists is None:
                db.add(KnowledgeRelation(tenant_id=principal.tenant_id, document_version_id=version_id,
                                         chunk_index=item.get("chunk_index") or 0, source=edge["source"],
                                         target=edge["target"], relation=edge["label"], excerpt=edge["excerpt"]))
        db.commit()


def require_admin(principal: Principal) -> None:
    if "admin" not in principal.roles:
        raise HTTPException(403, "admin role required")


def audit(db, principal: Principal, action: str, resource_type: str, resource_id: str | None, outcome: str):
    db.add(AuditEvent(
        tenant_id=principal.tenant_id,
        subject=principal.subject,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
    ))


@app.get("/", include_in_schema=False)
def root(request: Request):
    return RedirectResponse("/home" if browser_session_active(request) else "/login", status_code=303)


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    if browser_session_active(request):
        return RedirectResponse("/home", status_code=303)
    return FileResponse("web/login.html")


@app.get("/login/admin", include_in_schema=False)
def local_admin_login_page(request: Request):
    if browser_session_active(request):
        return RedirectResponse("/admin/", status_code=303)
    return FileResponse("web/local-admin-login.html")


@app.get("/home", include_in_schema=False)
def home(request: Request):
    if not browser_session_active(request):
        return RedirectResponse("/login", status_code=303)
    return FileResponse("web/index.html")


@app.get("/health")
def health():
    checks = {}
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "unavailable"
    try:
        cache.ping()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "unavailable"
    try:
        vector_client.get_collections()
        checks["qdrant"] = "ok"
    except Exception:
        checks["qdrant"] = "unavailable"
    if any(value != "ok" for value in checks.values()):
        raise HTTPException(503, {"status": "degraded", "checks": checks})
    return {"status": "ok", "checks": checks}


@app.get("/live")
def live():
    return {"status": "ok"}


@app.get("/auth/options")
def auth_options():
    mode = settings.auth_mode.lower()
    return {
        "local_login": mode in {"local", "oidc"},
        "idp_login": mode == "oidc",
        "registration_open": False,
    }


@app.get("/auth/login", include_in_schema=False)
def login(next: str = Query(default="/home")):
    destination = safe_next_path(next)
    if settings.auth_mode.lower() == "dev":
        response = RedirectResponse(destination, status_code=303)
        response.set_cookie(
            settings.identity_session_cookie,
            "dev",
            httponly=True,
            secure=settings.identity_cookie_secure,
            samesite="lax",
            max_age=settings.identity_session_max_seconds,
            path="/",
        )
        return response
    if settings.auth_mode.lower() == "local":
        return RedirectResponse("/login", status_code=303)
    if settings.auth_mode.lower() != "oidc":
        raise HTTPException(503, "browser login requires AUTH_MODE=oidc or dev")
    authorization_url, state, nonce, verifier = authorization_request()
    response = RedirectResponse(authorization_url, status_code=303)
    cookie_kwargs = {"httponly": True, "secure": settings.identity_cookie_secure,
                     "samesite": "lax", "max_age": 300, "path": "/"}
    response.set_cookie("oi_oidc_state", state, **cookie_kwargs)
    response.set_cookie("oi_oidc_nonce", nonce, **cookie_kwargs)
    response.set_cookie("oi_oidc_verifier", verifier, **cookie_kwargs)
    response.set_cookie("oi_oidc_next", destination, **cookie_kwargs)
    return response


@app.post("/auth/local/login", include_in_schema=False)
@app.post("/auth/local-admin/login", include_in_schema=False)
def local_login(request: Request, username: str = Form(...), password: str = Form(...),
                next: str = Form(default="/home")):
    if settings.auth_mode.lower() not in {"local", "oidc"}:
        raise HTTPException(503, "local login is disabled")
    require_same_origin(request)
    if not _local_login_attempt_allowed(request, username):
        raise HTTPException(429, "too many login attempts", headers={"Retry-After": "60"})
    principal = authenticate_local(username, password, settings.identity_default_tenant_id or "tenant-local")
    if principal is None:
        raise HTTPException(401, "Invalid local credentials")
    response = RedirectResponse(safe_next_path(next), status_code=303)
    response.set_cookie(settings.identity_session_cookie, _sign_session(principal), httponly=True,
                        secure=settings.identity_cookie_secure, samesite="lax",
                        max_age=settings.identity_session_max_seconds, path="/")
    return response


@app.get("/auth/callback", include_in_schema=False)
def callback(request: Request, code: str | None = None, state: str | None = None,
             error: str | None = None, error_description: str | None = None):
    if settings.auth_mode.lower() != "oidc":
        raise HTTPException(404, "OIDC login is disabled")
    if error:
        raise HTTPException(401, error_description or "OIDC login denied")
    if not code or not state or not hmac_compare(state, request.cookies.get("oi_oidc_state", "")):
        raise HTTPException(400, "Invalid OIDC state")
    nonce, verifier = request.cookies.get("oi_oidc_nonce"), request.cookies.get("oi_oidc_verifier")
    if not nonce or not verifier:
        raise HTTPException(400, "OIDC login has expired")
    try:
        principal = exchange_code(code, verifier, nonce)
        from app.auth import _database_principal
        principal = _database_principal(principal, provision=False)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, "OIDC provider unavailable") from exc
    response = RedirectResponse(safe_next_path(request.cookies.get("oi_oidc_next")), status_code=303)
    response.set_cookie(settings.identity_session_cookie, _sign_session(principal),
                        httponly=True, secure=settings.identity_cookie_secure,
                        samesite="lax", max_age=settings.identity_session_max_seconds, path="/")
    for cookie in ("oi_oidc_state", "oi_oidc_nonce", "oi_oidc_verifier", "oi_oidc_next"):
        response.delete_cookie(cookie, path="/")
    return response


@app.get("/auth/logout", include_in_schema=False)
def logout():
    # Provider outage must not leave a local session active.
    provider_url = "/login"
    if settings.auth_mode.lower() == "oidc":
        with suppress(Exception):
            provider_url = identity_logout_url()
    response = RedirectResponse(provider_url, status_code=303)
    response.delete_cookie(settings.identity_session_cookie, path="/")
    return response


@app.get("/account", include_in_schema=False)
def account(p: Principal = Depends(principal_from_session)):
    if p.source == "local":
        if not Path("web/account.html").exists():
            return Response(status_code=404)
        return FileResponse("web/account.html")
    return RedirectResponse(identity_account_url(), status_code=303)


@app.get("/api/v1/account")
def account_data(p: Principal = Depends(principal_from_session)):
    with SessionLocal() as db:
        identity = db.get(DbPrincipal, p.subject)
        username = None
        if identity is not None and identity.principal_type == "local_admin":
            credential = db.scalar(select(LocalAdminCredential).where(LocalAdminCredential.principal_id == p.subject))
            username = credential.username if credential else None
        elif identity is not None and identity.principal_type == "local_user":
            credential = db.scalar(select(LocalUserCredential).where(LocalUserCredential.principal_id == p.subject))
            username = credential.username if credential else None
        return {"subject": p.subject, "source": p.source, "username": username,
                "roles": sorted(p.roles)}


SUPPORTED_UPLOAD_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/markdown",
}
# ponytail: process-local 20-slot cap; use shared admission if multi-replica upload pressure matters.
# ponytail: two 100MB in-memory uploads per process fit the 2Gi API limit;
# use streaming persistence before increasing active upload concurrency.
_UPLOAD_SLOTS = asyncio.Semaphore(2)


def _content_type(filename: str, content_type: str | None) -> str:
    return content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _upload_metadata(file: UploadFile) -> tuple[str, str]:
    filename = Path((file.filename or "").replace("\\", "/")).name
    if not filename:
        raise HTTPException(400, "filename required")
    content_type = _content_type(filename, file.content_type)
    if content_type not in SUPPORTED_UPLOAD_TYPES:
        raise HTTPException(415, "unsupported document type")
    return filename, content_type


async def _read_document_upload(file: UploadFile, filename: str, content_type: str) -> bytes:
    raw = await read_upload(file, MAX_UPLOAD_BYTES)
    if not raw:
        raise HTTPException(400, "empty file")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file exceeds 100MB limit")
    return raw


def _persist_upload(p, filename, content_type, raw, roles, knowledge_base_name):
    document_id = str(uuid4())
    version_id = str(uuid4())
    source_hash = sha256(raw).hexdigest()
    key = f"{p.tenant_id}/documents/{document_id}/versions/1/source/{filename}"
    with SessionLocal() as db:
        ensure_principal(db, p)
        db.scalar(select(Tenant).where(Tenant.id == p.tenant_id).with_for_update())
        knowledge_base_id = None
        if knowledge_base_name:
            base = db.scalar(select(KnowledgeBase).where(
                KnowledgeBase.tenant_id == p.tenant_id,
                KnowledgeBase.name == knowledge_base_name,
            ))
            if base is None:
                base = KnowledgeBase(tenant_id=p.tenant_id, name=knowledge_base_name,
                                     created_by=p.subject)
                db.add(base)
                db.flush()
            knowledge_base_id = base.id
        duplicate = db.execute(select(Document, DocumentVersion).join(
            DocumentVersion, DocumentVersion.document_id == Document.id
        ).join(DocumentGrant, DocumentGrant.document_id == Document.id).join(Role, Role.id == DocumentGrant.role_id).where(
            Document.tenant_id == p.tenant_id,
            Document.knowledge_base_id == knowledge_base_id,
            DocumentVersion.source_hash == source_hash,
            Document.status == "ready",
            DocumentVersion.version == Document.version,
            DocumentVersion.status == "ready",
            Role.tenant_id == p.tenant_id,
            Role.name.in_(p.roles),
        ).distinct()).first()
        if duplicate:
            duplicate_doc, duplicate_version = duplicate
            if file_exists(duplicate_version.source_object_key):
                return {"document_id": duplicate_doc.id, "status": "reused"}

        doc = Document(id=document_id, tenant_id=p.tenant_id, title=filename, status="queued",
                       created_by=p.subject, knowledge_base_id=knowledge_base_id, object_key=key,
                       content_type=content_type)
        version = DocumentVersion(id=version_id, document_id=document_id, version=1,
                                 source_object_key=key, source_filename=filename,
                                 content_type=content_type, source_hash=source_hash, created_by=p.subject)
        db.add_all([doc, version])
        db.flush()
        for role_name in roles:
            role = db.scalar(select(Role).where(Role.tenant_id == p.tenant_id, Role.name == role_name))
            if role is None:
                role = Role(id=str(uuid4()), tenant_id=p.tenant_id, name=role_name)
                db.add(role)
                db.flush()
            db.add(DocumentGrant(document_id=document_id, role_id=role.id))
        commit_started = False
        try:
            put_file(key, BytesIO(raw), content_type)
            enqueue_document(db, doc, version, trace_id=str(uuid4()))
            bump_knowledge_revision(p.tenant_id, db)
            audit(db, p, "document.upload", "document", document_id, "accepted")
            commit_started = True
            db.commit()
        except Exception as exc:
            db.rollback()
            # A failed commit can be ambiguous; retain the object for orphan
            # cleanup instead of deleting a source with a durable outbox row.
            if not commit_started:
                with suppress(Exception):
                    delete_file(key)
            raise HTTPException(503, "document storage unavailable") from exc
    return {"document_id": document_id, "document_version_id": version_id, "status": "queued"}


@app.post("/api/v1/documents", status_code=202)
async def upload_document(
    file: UploadFile = File(...),
    roles: str | None = Form(default=None),
    knowledge_base: str | None = Form(default=None),
    p: Principal = Depends(principal_from_session),
):
    filename, content_type = _upload_metadata(file)
    requested_roles = {x.strip() for x in (roles or "").split(",") if x.strip()}
    knowledge_base_name = (knowledge_base or "").strip()
    if len(knowledge_base_name) > 255:
        raise HTTPException(422, "knowledge base name exceeds 255 characters")
    if requested_roles and "admin" not in p.roles:
        raise HTTPException(403, "only admin can assign document roles")
    if knowledge_base_name and "admin" not in p.roles:
        raise HTTPException(403, "only admin can assign a knowledge base")
    effective_roles = requested_roles or set(p.roles)
    if not effective_roles:
        raise HTTPException(403, "at least one document role required")
    async with _UPLOAD_SLOTS:
        raw = await _read_document_upload(file, filename, content_type)
        return await run_sync(_persist_upload, p, filename, content_type, raw, effective_roles, knowledge_base_name)


def document_access(p: Principal):
    return (
        Document.tenant_id == p.tenant_id,
        Document.status.notin_(["deleted", "deleting"]),
        select(DocumentGrant.document_id).join(Role, Role.id == DocumentGrant.role_id).where(
            DocumentGrant.document_id == Document.id,
            Role.tenant_id == p.tenant_id,
            Role.name.in_(p.roles),
        ).exists(),
    )


def _document_grant_roles(db, document_id: str, tenant_id: str) -> set[str]:
    return set(db.scalars(select(Role.name).join(
        DocumentGrant, DocumentGrant.role_id == Role.id
    ).where(DocumentGrant.document_id == document_id, Role.tenant_id == tenant_id)))


def _sync_document_acl(document: Document, roles: set[str]) -> None:
    vector_client.set_payload(
        settings.qdrant_collection,
        {"allowed_roles": sorted(roles)},
        models.FilterSelector(filter=models.Filter(must=[
            models.FieldCondition(key="tenant_id", match=models.MatchValue(value=document.tenant_id)),
            models.FieldCondition(key="document_id", match=models.MatchValue(value=document.id)),
        ])),
        wait=True,
    )


def _list_documents(p: Principal, offset: int, limit: int, *, administrative: bool = False):
    conditions = (Document.tenant_id == p.tenant_id, Document.status.notin_(["deleted", "deleting"])) \
        if administrative else document_access(p)
    with SessionLocal() as db:
        total = db.scalar(select(func.count()).select_from(Document).where(*conditions))
        rows = db.execute(select(Document, KnowledgeBase.name).outerjoin(
            KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id
        ).where(
            *conditions
        ).order_by(Document.created_at.desc(), Document.id).offset(offset).limit(limit)).all()
        return {"items": [{"id": doc.id, "title": doc.title, "version": doc.version,
                            "knowledge_base": base_name, "content_type": doc.content_type,
                            "status": doc.status, "error": doc.error,
                            "created_at": doc.created_at.isoformat()}
                           for doc, base_name in rows], "total": total}


@app.get("/api/v1/documents")
def list_documents(p: Principal = Depends(principal_from_session),
                   offset: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=200)):
    return _list_documents(p, offset, limit)


@app.get("/api/v1/documents/{document_id}/status")
def document_status(document_id: str, p: Principal = Depends(principal_from_session)):
    with SessionLocal() as db:
        doc = db.scalar(select(Document).where(Document.id == document_id, *document_access(p)))
        if doc is None:
            raise HTTPException(404, "document not found")
        return {"id": doc.id, "title": doc.title, "status": doc.status, "error": doc.error}


@app.post("/api/v1/documents/{document_id}/reindex")
def reindex_document(document_id: str, p: Principal = Depends(principal_from_session)):
    require_admin(p)
    with SessionLocal() as db:
        doc = db.scalar(select(Document).where(
            Document.id == document_id, Document.tenant_id == p.tenant_id
        ).with_for_update())
        if doc is None:
            raise HTTPException(404, "document not found")
        if doc.status in {"deleted", "deleting"}:
            raise HTTPException(409, "deleted document cannot be reindexed")
        version = db.scalar(select(DocumentVersion).where(
            DocumentVersion.document_id == doc.id, DocumentVersion.version == doc.version
        ).with_for_update())
        if version is None:
            raise HTTPException(409, "document version missing")
        doc.status, doc.error, version.status = "queued", None, "queued"
        enqueue_document(db, doc, version, trace_id=str(uuid4()))
        bump_knowledge_revision(p.tenant_id, db)
        audit(db, p, "document.reindex", "document", document_id, "accepted")
        db.commit()
    return {"document_id": document_id, "status": "queued"}


@app.post("/api/v1/admin/documents/reindex-all")
def reindex_all_documents(p: Principal = Depends(principal_from_session)):
    require_admin(p)
    with SessionLocal() as db:
        documents = db.scalars(select(Document).where(
            Document.tenant_id == p.tenant_id, Document.status.notin_(["deleted", "deleting"])
        ).order_by(Document.id).with_for_update()).all()
        queued = 0
        for document in documents:
            version = db.scalar(select(DocumentVersion).where(
                DocumentVersion.document_id == document.id, DocumentVersion.version == document.version
            ).with_for_update())
            if version is None:
                continue
            document.status, document.error, version.status = "queued", None, "queued"
            enqueue_document(db, document, version, trace_id=str(uuid4()))
            audit(db, p, "document.reindex_all", "document", document.id, "accepted")
            queued += 1
        if queued:
            bump_knowledge_revision(p.tenant_id, db)
        db.commit()
    return {"queued": queued}


@app.post("/api/v1/documents/{document_id}/cancel")
def cancel_document(document_id: str, p: Principal = Depends(principal_from_session)):
    require_admin(p)
    with SessionLocal() as db:
        doc = db.scalar(select(Document).where(
            Document.id == document_id, Document.tenant_id == p.tenant_id
        ).with_for_update())
        if doc is None:
            raise HTTPException(404, "document not found")
        if doc.status in {"ready", "deleted", "deleting", "canceled"}:
            raise HTTPException(409, f"cannot cancel document in status {doc.status}")
        doc.status, doc.error = "canceled", "任务由管理员取消"
        version = db.scalar(select(DocumentVersion).where(
            DocumentVersion.document_id == doc.id, DocumentVersion.version == doc.version
        ).with_for_update())
        if version is not None and version.status not in {"ready", "deleted"}:
            version.status = "canceled"
        bump_knowledge_revision(p.tenant_id, db)
        audit(db, p, "document.cancel", "document", document_id, "accepted")
        db.commit()
    return {"document_id": document_id, "status": "canceled"}


@app.delete("/api/v1/documents/{document_id}", status_code=202)
def delete_document(document_id: str, p: Principal = Depends(principal_from_session)):
    require_admin(p)
    with SessionLocal() as db:
        doc = db.scalar(select(Document).where(
            Document.id == document_id, Document.tenant_id == p.tenant_id
        ).with_for_update())
        if doc is None:
            raise HTTPException(404, "document not found")
        if doc.status == "deleted":
            return {"document_id": document_id, "status": "deleted"}
        retrying = doc.status == "deleting"
        doc.status, doc.error = "deleting", None
        statement = select(DocumentVersion).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.status != "deleted",
        )
        if retrying:
            # Retry only exhausted cleanup; repeated DELETE must not steal a live attempt.
            statement = statement.join(IngestionJob, IngestionJob.document_version_id == DocumentVersion.id).where(
                IngestionJob.stage == "delete", IngestionJob.status == "dead_letter")
        versions = db.scalars(statement.with_for_update(of=DocumentVersion)).all()
        for version in versions:
            enqueue_document(db, doc, version, event_type="document.delete", trace_id=str(uuid4()))
        if not versions and not retrying:
            doc.status, doc.deleted_at = "deleted", func.now()
        bump_knowledge_revision(p.tenant_id, db)
        audit(db, p, "document.delete", "document", document_id, "accepted")
        db.commit()
        status = doc.status
    return {"document_id": document_id, "status": status}


@app.post("/api/v1/retrieval/search")
@app.post("/search")
async def retrieve(body: SearchRequest, request: Request, p: Principal = Depends(principal_from_session)):
    async with admit(p):
        return await run_cancellable(_retrieve(body, p), request)


async def _retrieve(body: SearchRequest, p: Principal):
    started = perf_counter()
    revision = await run_sync(query_revision, p.tenant_id)
    key = cache_key("retrieval", CACHE_CONTRACT_VERSION, p.tenant_id, sorted(p.roles), revision,
                    normalize_query(body.query), body.limit, settings.embedding_model)
    cached = await run_sync(cache_get, key)
    if cached is not None:
        await run_sync(validate_query_result, p, revision, cached)
        return cached
    if not p.roles:
        result = {"results": []}
        await run_sync(cache_put, key, result, 30)
        await run_sync(log_query, p, body.query, "retrieval", 0, started)
        return result
    vector = (await async_llm.embeddings.create(model=settings.embedding_model, input=body.query)).data[0].embedding
    aliases, records = await run_sync(approved_aliases, p, body.query)
    points = await search_async(vector, p.tenant_id, set(p.roles), body.limit, body.query, aliases)
    contexts = [point.payload or {} for point in points]
    result = {"results": [dict(point.payload or {}, score=point.score) for point in points],
              "entity_bindings": alias_bindings(contexts, records)}
    await run_sync(validate_query_result, p, revision, result)
    await run_sync(cache_put, key, result, 120)
    await run_sync(log_query, p, body.query, "retrieval", len(points), started)
    return result


def _answer_result(answer: str, state: str, citations: list[dict], contract: list[dict],
                   revision: int, bindings: list[dict] | None = None) -> dict:
    return {"answer_id": str(uuid4()), "answer": answer, "answer_state": state,
            "answer_state_label": ANSWER_STATE_LABELS[state], "evidence_contract": contract,
            "citations": citations, "entity_bindings": bindings or [], "graph_available": state == "answered",
            "cache": {"level": "generated", "hit": False, "knowledge_revision": revision}}


def _cache_result(result: dict, level: str, revision: int) -> dict:
    copy = dict(result)
    copy["cache"] = {"level": level, "hit": True, "knowledge_revision": revision}
    return copy


def _feedback_token(answer_id: str, p: Principal) -> str:
    secret = (settings.identity_session_secret or settings.jwt_secret).encode()
    message = f"{p.tenant_id}|{p.subject}|{answer_id}".encode()
    return hmac.new(secret, message, sha256).hexdigest()


def validate_query_result(p: Principal, revision: int, result: dict) -> None:
    """Caches and slow generation cannot bypass current document authorization."""
    mode = settings.auth_mode.lower()
    revalidate = ((mode == "local" and p.source == "local")
                  or (mode == "oidc" and p.source in {"local", "idp", "service"})
                  or (mode == "jwt" and p.source == "service"))
    if revalidate:
        from app.auth import _database_principal
        current = _database_principal(
            p, provision=False, service_only=p.source == "service",
            check_session=p.source in {"local", "idp"},
        )
        if current.roles != p.roles:
            raise HTTPException(409, "Access changed; retry the query")
    # Unrelated ingestion must not starve long answers. Cache writes retain
    # their original revision key; validate only the evidence actually used.
    query_revision(p.tenant_id)
    evidence = result.get("results", result.get("citations", []))
    bindings = result.get("entity_bindings", [])
    if not evidence and not bindings:
        return
    keys = {(item.get("document_id"), item.get("document_version")) for item in evidence}
    with SessionLocal() as db:
        visible = set(db.execute(select(Document.id, DocumentVersion.version).join(
            DocumentVersion, DocumentVersion.document_id == Document.id
        ).where(*document_access(p), Document.status == "ready",
                DocumentVersion.version == Document.version, DocumentVersion.status == "ready",
                Document.id.in_([key[0] for key in keys]))).all())
        if bindings:
            binding_ids = {item.get("alias_id") for item in bindings}
            approved = db.execute(select(EntityAlias.id, Document.id, DocumentVersion.version).join(
                DocumentVersion, DocumentVersion.id == EntityAlias.document_version_id
            ).join(Document, Document.id == DocumentVersion.document_id).where(
                EntityAlias.id.in_(binding_ids), EntityAlias.tenant_id == p.tenant_id,
                EntityAlias.status == "approved", *document_access(p),
                Document.status == "ready", DocumentVersion.status == "ready",
                DocumentVersion.version == Document.version,
            )).all()
            if binding_ids != {row[0] for row in approved if (row[1], row[2]) in keys}:
                raise HTTPException(409, "Alias evidence changed; retry the query")
    if not keys.issubset(visible):
        raise HTTPException(409, "Evidence access changed; retry the query")


def query_revision(tenant_id: str) -> int:
    value = knowledge_revision(tenant_id)
    if not isinstance(value, int):
        raise HTTPException(503, "Knowledge authorization unavailable")
    return value


def _with_feedback(result: dict, p: Principal) -> dict:
    validate_query_result(p, result["cache"]["knowledge_revision"], result)
    copy = dict(result)
    with SessionLocal() as db:
        copy["liked"] = db.scalar(select(func.count()).select_from(AnswerFeedback).where(
            AnswerFeedback.tenant_id == p.tenant_id,
            AnswerFeedback.subject == p.subject,
            AnswerFeedback.answer_id == result["answer_id"],
        )) > 0
    copy["feedback_token"] = _feedback_token(result["answer_id"], p)
    return copy


async def _answer_work(body: AnswerRequest, p: Principal, revision: int,
                       exact_key: str, semantic_bucket: str, aliases: dict[str, list[str]], records: list[dict]) -> dict:
    started = perf_counter()
    if not p.roles:
        await run_sync(log_query, p, body.query, "answer", 0, started)
        result = _answer_result("没有找到当前角色可访问的知识。", "no_answer", [], [], revision)
        await run_sync(cache_put, exact_key, result, 300)
        return result
    vector = (await async_llm.embeddings.create(
        model=settings.embedding_model, input=body.query
    )).data[0].embedding
    points = await search_async(vector, p.tenant_id, set(p.roles), min(body.limit, 3), body.query, aliases)
    contexts = [point.payload or {} for point in points]
    if not contexts:
        await run_sync(log_query, p, body.query, "answer", 0, started)
        result = _answer_result("根据当前可访问的知识，我无法确认这个问题。",
                                "no_answer", [], [], revision)
        await run_sync(cache_put, exact_key, result, 300)
        return result
    entities = query_entities(body.query)
    evidence = evidence_signature(contexts)
    similar = await run_sync(semantic_get, semantic_bucket, vector, entities, evidence)
    if similar is not None:
        result = _cache_result(similar, "l2", revision)
        await run_sync(validate_query_result, p, revision, result)
        await run_sync(cache_put, exact_key, similar, 21600)
        await run_sync(log_query, p, body.query, "answer:l2", len(result.get("citations", [])), started)
        return result
    context_text = "\n\n".join(
        f"[证据 {i + 1}] 文档：{item.get('title')}，章节块：{item.get('chunk_index')}\n{item.get('content', '')[:1400]}"
        for i, item in enumerate(contexts)
    )
    completion = await async_llm.chat.completions.create(
        model=settings.chat_model,
        temperature=body.temperature,
        max_tokens=600,
        reasoning_effort="none",
        messages=[
            {"role": "system", "content": "你是严格的检索问答助手。只回答问题本身，只能使用证据。禁止引入证据中没有的人物、事件或数字。不要把‘军师/谋士’推断成‘师徒/师生’，不要把合作关系改写成亲属关系。严格输出三段：\n## 结论\n## 归纳\n## 依据\n结论中的每一条陈述末尾必须直接标注支持它的 [证据 N]；依据也必须引用证据编号。无法确认就写‘资料不足’，不要编造。"},
            {"role": "user", "content": f"问题：{body.query}\n\n证据：\n{context_text}"},
        ],
    )
    answer_text = completion.choices[0].message.content or ""
    if "## 结论" not in answer_text or "## 依据" not in answer_text:
        answer_text = "## 结论\n模型未生成可验证的归纳，以下仅返回原文证据。\n\n## 归纳\n- 请根据下方证据进行人工确认。\n\n## 依据\n" + "\n".join(
            f"[证据 {i + 1}] {item.get('content', '')[:800]}" for i, item in enumerate(contexts)
        )
    citations = citations_for(points, contexts)
    state = answer_state(answer_text, contexts)
    claims = _conclusion_claims(answer_text)
    pair_support = direct_pair_support(body.query, claims, contexts, records)
    relationship_support = ["indirect" if pair is False else None for pair in pair_support]
    contract = evidence_contract(answer_text, contexts, state, relationship_support)
    if any(support == "indirect" for support in relationship_support):
        direct_contract = [item for item in contract if item["support"] == "supported"]
        answer_text, state = direct_only_answer(contract)
        contract = direct_contract or evidence_contract(answer_text, contexts, state)
    result = _answer_result(answer_text, state, citations, contract, revision, alias_bindings(contexts, records))
    await run_sync(validate_query_result, p, revision, result)
    await run_sync(cache_put, exact_key, result, 21600 if state == "answered" else 300)
    if state == "answered" and contract and all(
            item["support"] == "supported" and item["confidence"] >= 0.85 for item in contract):
        await run_sync(semantic_put, semantic_bucket, vector, entities, evidence, result)
    await run_sync(log_query, p, body.query, "answer", len(citations), started)
    return result


async def _answer(body: AnswerRequest, p: Principal):
    revision = await run_sync(query_revision, p.tenant_id)
    scope = (p.tenant_id, sorted(p.roles), revision, body.limit, body.temperature,
             settings.embedding_model, settings.chat_model, "answer-prompt-v3")
    exact_key = cache_key("answer", CACHE_CONTRACT_VERSION, *scope, normalize_query(body.query))
    cached = await run_sync(cache_get, exact_key)
    if cached is not None:
        return await run_sync(_with_feedback, _cache_result(cached, "l1", revision), p)
    aliases, records = await run_sync(approved_aliases, p, body.query)
    semantic_bucket = cache_key("answer-semantic", CACHE_CONTRACT_VERSION, *scope)
    result, joined = await singleflight(
        exact_key, lambda: _answer_work(body, p, revision, exact_key, semantic_bucket, aliases, records)
    )
    result = _cache_result(result, "l0", revision) if joined else result
    return await run_sync(_with_feedback, result, p)


async def _graph(body: GraphRequest, p: Principal):
    started = perf_counter()
    revision = await run_sync(query_revision, p.tenant_id)
    key = cache_key("graph", CACHE_CONTRACT_VERSION, p.tenant_id, sorted(p.roles), revision,
                    normalize_query(body.query), body.limit,
                    settings.embedding_model, settings.chat_model)
    cached = await run_sync(cache_get, key)
    if cached is not None:
        await run_sync(validate_query_result, p, revision, cached)
        return cached
    if not p.roles:
        return {"nodes": [], "edges": [], "citations": [], "message": "没有可访问的知识。"}
    vector = (await async_llm.embeddings.create(model=settings.embedding_model, input=body.query)).data[0].embedding
    aliases, records = await run_sync(approved_aliases, p, body.query)
    points = await search_async(vector, p.tenant_id, set(p.roles), min(body.limit, 3), body.query, aliases)
    contexts = [point.payload or {} for point in points]
    if not contexts:
        await run_sync(log_query, p, body.query, "graph", 0, started)
        return {"nodes": [], "edges": [], "citations": [], "message": "没有找到可建立关系的原文证据。"}
    evidence = "\n\n".join(
        f"[证据 {index + 1}] {item.get('title')}，第 {item.get('chunk_index')} 段\n{item.get('content', '')[:720]}"
        for index, item in enumerate(contexts)
    )
    alias_note = "；".join(f"{canonical} 可由原文称谓 {', '.join(item if isinstance(item, str) else item['alias'] for item in values)} 指代" for canonical, values in aliases.items())
    completion = await async_llm.chat.completions.create(
        model=settings.chat_model,
        temperature=0,
        max_tokens=420,
        reasoning_effort="none",
        messages=[
            {"role": "system", "content": "你是原文证据关系抽取器。只提取问题点名实体之间、且由证据直接说明的关系，不返回其他实体对，绝不补充常识或推断。实体必须逐字出现在所引证据中。关系可以是技术、流程、组织或业务领域的任意简洁术语（例如 depends_on、owned_by、approved_by、triggers、blocks）；保留原文明确术语，不要强行套用小型固定词表。只输出 JSON 数组，最多 8 项，不要 Markdown：[{\"source\":\"实体甲\",\"target\":\"实体乙\",\"relation\":\"关系\",\"evidence\":1}]。evidence 是证据编号。"},
            {"role": "user", "content": f"问题：{body.query}\n\n已审核原文称谓：{alias_note or '无'}\n\n证据：\n{evidence}\n\n只输出 JSON 数组，不要解释、标题或 Markdown。"},
        ],
    )
    extracted = parse_relationships(completion.choices[0].message.content or "", contexts, body.query, aliases)
    statements = [f"{edge['source']} | {edge['label']} | {edge['target']}" for edge in extracted]
    pairs = direct_pair_support(body.query, statements, contexts, records)
    extracted = [edge for edge, pair in zip(extracted, pairs) if pair is not False]
    await run_sync(persist_relationships, p, contexts, extracted)
    edges = merge_relationships(extracted)
    names = []
    for edge in edges:
        for name in (edge["source"], edge["target"]):
            if name not in names:
                names.append(name)
    result = {"nodes": [{"id": name, "label": name} for name in names], "edges": edges,
              "citations": citations_for(points, contexts),
              "entity_bindings": alias_bindings(contexts, records),
              "message": "" if edges else "当前证据不足以确认人物或实体关系。"}
    await run_sync(validate_query_result, p, revision, result)
    await run_sync(cache_put, key, result, 300)
    await run_sync(log_query, p, body.query, "graph", len(edges), started)
    return result


async def run_cancellable(work, request: Request):
    task = asyncio.create_task(work)
    try:
        while not task.done():
            await asyncio.wait((task,), timeout=0.1)
            if await request.is_disconnected():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                return Response(status_code=499)
        return task.result()
    except asyncio.CancelledError:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        raise


@app.post("/api/v1/rag/answer")
async def answer(body: AnswerRequest, request: Request,
                 p: Principal = Depends(principal_from_session)):
    async with admit(p):
        return await run_cancellable(_answer(body, p), request)


@app.post("/api/v1/rag/graph")
async def graph(body: GraphRequest, request: Request,
                p: Principal = Depends(principal_from_session)):
    async with admit(p):
        return await run_cancellable(_graph(body, p), request)


@app.put("/api/v1/rag/answers/{answer_id}/feedback")
def set_answer_feedback(answer_id: str, body: FeedbackRequest,
                        p: Principal = Depends(principal_from_session)):
    try:
        answer_id = str(UUID(answer_id))
    except ValueError as exc:
        raise HTTPException(422, "invalid answer id") from exc
    if not hmac.compare_digest(body.token, _feedback_token(answer_id, p)):
        raise HTTPException(403, "invalid feedback token")
    with SessionLocal() as db:
        ensure_principal(db, p)
        item = db.scalar(select(AnswerFeedback).where(
            AnswerFeedback.tenant_id == p.tenant_id,
            AnswerFeedback.subject == p.subject,
            AnswerFeedback.answer_id == answer_id,
        ))
        if body.liked and item is None:
            db.add(AnswerFeedback(tenant_id=p.tenant_id, subject=p.subject,
                                  answer_id=answer_id, liked=1))
        elif not body.liked and item is not None:
            db.delete(item)
        db.commit()
    return {"answer_id": answer_id, "liked": body.liked}


@app.post("/api/v1/admin/roles")
def create_role(body: RoleRequest, p: Principal = Depends(principal_from_session)):
    require_admin(p)
    with SessionLocal() as db:
        ensure_principal(db, p)
        role = db.scalar(select(Role).where(Role.tenant_id == p.tenant_id, Role.name == body.name))
        if role is None:
            role = Role(id=str(uuid4()), tenant_id=p.tenant_id, name=body.name)
            db.add(role)
            db.commit()
        return {"id": role.id, "name": role.name}


def _validated_roles(roles: set[str]) -> set[str]:
    values = {name.strip() for name in roles if isinstance(name, str) and name.strip()}
    if any(not re.fullmatch(r"[a-zA-Z0-9:_-]{1,128}", name) for name in values):
        raise HTTPException(422, "invalid role name")
    return values


@app.post("/api/v1/admin/members", status_code=201)
def create_member(request: Request, body: MemberCreateRequest,
                  p: Principal = Depends(principal_from_session)):
    require_admin(p)
    require_same_origin(request)
    roles = _validated_roles(body.roles)
    with SessionLocal() as db:
        tenant = db.get(Tenant, p.tenant_id)
        if tenant is None or tenant.status != "active":
            raise HTTPException(403, "tenant is disabled")
        if body.source == "local":
            try:
                username = normalize_username(body.username or "")
                password = validate_password(body.password or "")
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            if db.scalar(select(LocalAdminCredential).where(LocalAdminCredential.username == username)) is not None:
                raise HTTPException(409, "username already exists")
            if db.scalar(select(LocalUserCredential).where(
                    LocalUserCredential.tenant_id == p.tenant_id,
                    LocalUserCredential.username == username)) is not None:
                raise HTTPException(409, "username already exists")
            subject = f"local:user:{uuid4()}"
            member = DbPrincipal(id=subject, tenant_id=p.tenant_id, display_name=username,
                                 principal_type="local_user")
            db.add(member)
            db.flush()
            db.add(LocalUserCredential(principal_id=subject, tenant_id=p.tenant_id,
                                       username=username, password_hash=hash_password(password)))
        else:
            subject = (body.subject or "").strip()
            if not subject or len(subject) > 256:
                raise HTTPException(422, "invalid IdP subject")
            member = db.get(DbPrincipal, subject)
            if member is not None:
                if member.tenant_id != p.tenant_id or member.principal_type != "idp":
                    raise HTTPException(409, "member belongs to another source or tenant")
                if member.status != "active":
                    raise HTTPException(409, "member is disabled")
            else:
                member = DbPrincipal(id=subject, tenant_id=p.tenant_id, display_name=None,
                                     principal_type="idp")
                db.add(member)
                db.flush()
        for name in roles:
            role = db.scalar(select(Role).where(Role.tenant_id == p.tenant_id, Role.name == name))
            if role is None:
                role = Role(id=str(uuid4()), tenant_id=p.tenant_id, name=name)
                db.add(role)
                db.flush()
            db.add(PrincipalRole(principal_id=member.id, role_id=role.id))
        audit(db, p, "member.create", "principal", member.id, "accepted")
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(409, "member already exists") from exc
        return {"subject": member.id, "source": body.source, "roles": sorted(roles)}


@app.get("/api/v1/admin/members")
def list_members(p: Principal = Depends(principal_from_session)):
    require_admin(p)
    with SessionLocal() as db:
        members = db.scalars(select(DbPrincipal).where(DbPrincipal.tenant_id == p.tenant_id)
                             .order_by(DbPrincipal.created_at.desc())).all()
        assignments = db.execute(select(PrincipalRole.principal_id, Role.name).join(
            Role, Role.id == PrincipalRole.role_id
        ).where(Role.tenant_id == p.tenant_id)).all()
        roles_by_subject: dict[str, list[str]] = {}
        for subject, name in assignments:
            roles_by_subject.setdefault(subject, []).append(name)
        usernames = {}
        for credential in db.scalars(select(LocalAdminCredential).where(
                LocalAdminCredential.principal_id.in_([member.id for member in members]))).all():
            usernames[credential.principal_id] = credential.username
        for credential in db.scalars(select(LocalUserCredential).where(
                LocalUserCredential.principal_id.in_([member.id for member in members]))).all():
            usernames[credential.principal_id] = credential.username
        source_names = {"local_admin": "local", "local_user": "local", "idp": "idp",
                        "service_account": "service"}
        return {"items": [{
            "subject": member.id,
            "source": source_names.get(member.principal_type, member.principal_type),
            "username": usernames.get(member.id),
            "display_name": member.display_name,
            "status": member.status,
            "roles": sorted(roles_by_subject.get(member.id, [])),
            "initial_local_admin": member.principal_type == "local_admin",
        } for member in members], "roles": db.scalars(select(Role.name).where(
            Role.tenant_id == p.tenant_id).order_by(Role.name)).all()}


def managed_idp_member(db, principal: Principal, subject: str) -> DbPrincipal:
    member = db.scalar(select(DbPrincipal).where(
        DbPrincipal.id == subject, DbPrincipal.tenant_id == principal.tenant_id
    ).with_for_update())
    if member is None:
        raise HTTPException(404, "member not found")
    if member.principal_type == "local_admin":
        raise HTTPException(400, "The installation administrator is not managed here")
    if subject == principal.subject:
        raise HTTPException(409, "Administrators cannot change their own access")
    return member


@app.put("/api/v1/admin/members/{subject}/roles")
def update_member_roles(request: Request, subject: str, body: MemberRolesRequest,
                        p: Principal = Depends(principal_from_session)):
    require_admin(p)
    require_same_origin(request)
    roles = _validated_roles(body.roles)
    with SessionLocal() as db:
        member = managed_idp_member(db, p, subject)
        existing = db.scalars(select(PrincipalRole).join(Role, Role.id == PrincipalRole.role_id).where(
            PrincipalRole.principal_id == member.id, Role.tenant_id == p.tenant_id
        )).all()
        for assignment in existing:
            db.delete(assignment)
        for name in roles:
            role = db.scalar(select(Role).where(Role.tenant_id == p.tenant_id, Role.name == name))
            if role is None:
                role = Role(id=str(uuid4()), tenant_id=p.tenant_id, name=name)
                db.add(role)
                db.flush()
            db.add(PrincipalRole(principal_id=member.id, role_id=role.id))
        audit(db, p, "member.roles.update", "principal", member.id, "accepted")
        db.commit()
    return {"subject": subject, "roles": sorted(roles)}


@app.put("/api/v1/admin/members/{subject}/status")
def update_member_status(request: Request, subject: str, body: MemberStatusRequest,
                         p: Principal = Depends(principal_from_session)):
    require_admin(p)
    require_same_origin(request)
    with SessionLocal() as db:
        member = managed_idp_member(db, p, subject)
        member.status = body.status
        member.session_version += 1
        audit(db, p, "member.status.update", "principal", member.id, "accepted")
        db.commit()
    return {"subject": subject, "status": body.status}


@app.post("/api/v1/admin/members/{subject}/password")
def reset_member_password(request: Request, subject: str, body: PasswordResetRequest,
                          p: Principal = Depends(principal_from_session)):
    require_admin(p)
    require_same_origin(request)
    try:
        password = validate_password(body.new_password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    with SessionLocal() as db:
        member = managed_idp_member(db, p, subject)
        if member.principal_type != "local_user":
            raise HTTPException(400, "only ordinary local users have local passwords")
        credential = db.scalar(select(LocalUserCredential).where(
            LocalUserCredential.principal_id == subject
        ).with_for_update())
        if credential is None:
            raise HTTPException(409, "local credential is missing")
        credential.password_hash = hash_password(password)
        member.session_version += 1
        audit(db, p, "member.password.reset", "principal", member.id, "accepted")
        db.commit()
    return {"subject": subject, "reset": True}


@app.post("/api/v1/account/password")
def change_account_password(request: Request, body: AccountPasswordRequest,
                            response: Response,
                            p: Principal = Depends(principal_from_session)):
    require_same_origin(request)
    if p.source != "local":
        raise HTTPException(400, "only local accounts have local passwords")
    with SessionLocal() as db:
        member = db.scalar(select(DbPrincipal).where(DbPrincipal.id == p.subject).with_for_update())
        if member is None or member.status != "active" or member.principal_type not in {"local_admin", "local_user"}:
            raise HTTPException(403, "local account is unavailable")
        if member.session_version != p.session_version:
            raise HTTPException(401, "Session expired")
        if member.principal_type == "local_admin":
            credential = db.scalar(select(LocalAdminCredential).where(
                LocalAdminCredential.principal_id == p.subject
            ).with_for_update())
        else:
            credential = db.scalar(select(LocalUserCredential).where(
                LocalUserCredential.principal_id == p.subject
            ).with_for_update())
        current_password_max_length = (LOCAL_ADMIN_PASSWORD_MAX_LENGTH
                                       if member.principal_type == "local_admin" else 128)
        try:
            new_password = validate_password(body.new_password)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if (credential is None or not isinstance(body.current_password, str)
                or not 16 <= len(body.current_password) <= current_password_max_length
                or not verify_password(body.current_password, credential.password_hash)):
            raise HTTPException(401, "invalid current password")
        credential.password_hash = hash_password(new_password)
        member.session_version += 1
        audit(db, p, "account.password.change", "principal", member.id, "accepted")
        db.commit()
    response.delete_cookie(settings.identity_session_cookie, path="/")
    return {"ok": True, "reauthenticate": True}


@app.get("/api/v1/admin/stats")
def stats(p: Principal = Depends(principal_from_session)):
    require_admin(p)
    with SessionLocal() as db:
        return {"documents": db.scalar(select(func.count()).select_from(Document).where(Document.tenant_id == p.tenant_id, Document.status != "deleted")),
                "ready": db.scalar(select(func.count()).select_from(Document).where(Document.tenant_id == p.tenant_id, Document.status == "ready")),
                "failed": db.scalar(select(func.count()).select_from(Document).where(Document.tenant_id == p.tenant_id, Document.status == "failed"))}


@app.get("/api/v1/admin/overview")
def admin_overview(p: Principal = Depends(principal_from_session)):
    require_admin(p)
    with SessionLocal() as db:
        base = Document.tenant_id == p.tenant_id
        return {
            "documents": db.scalar(select(func.count()).select_from(Document).where(base, Document.status != "deleted")) or 0,
            "chunks": vector_client.count(settings.qdrant_collection, count_filter=models.Filter(must=[models.FieldCondition(key="tenant_id", match=models.MatchValue(value=p.tenant_id)), models.FieldCondition(key="allowed_roles", match=models.MatchAny(any=list(p.roles)))]), exact=True).count or 0,
            "query_calls": db.scalar(select(func.count()).select_from(QueryEvent).where(QueryEvent.tenant_id == p.tenant_id)) or 0,
            "completed_tasks": db.scalar(select(func.count()).select_from(Document).where(base, Document.status == "ready")) or 0,
            "in_progress_tasks": db.scalar(select(func.count()).select_from(Document).where(base, Document.status.in_(["received", "queued", "processing"]))) or 0,
            "users": db.scalar(select(func.count()).select_from(DbPrincipal).where(DbPrincipal.tenant_id == p.tenant_id)) or 0,
            "failed_tasks": db.scalar(select(func.count()).select_from(Document).where(base, Document.status == "failed")) or 0,
        }


@app.get("/api/v1/questions/top")
def top_questions(p: Principal = Depends(principal_from_session)):
    with SessionLocal() as db:
        rows = db.execute(select(QueryEvent.query, func.count(QueryEvent.id).label("count"))
                         .where(QueryEvent.tenant_id == p.tenant_id, QueryEvent.subject == p.subject)
                         .group_by(QueryEvent.query).order_by(func.count(QueryEvent.id).desc()).limit(10)).all()
        return {"items": [{"question": query, "count": count} for query, count in rows]}


@app.get("/api/v1/admin/questions")
def admin_questions(p: Principal = Depends(principal_from_session)):
    require_admin(p)
    with SessionLocal() as db:
        rows = db.execute(select(QueryEvent.query, func.count(QueryEvent.id).label("count"))
                         .where(QueryEvent.tenant_id == p.tenant_id)
                         .group_by(QueryEvent.query).order_by(func.count(QueryEvent.id).desc()).limit(20)).all()
        return {"items": [{"question": query, "count": count} for query, count in rows]}


@app.get("/api/v1/admin/notifications")
def admin_notifications(p: Principal = Depends(principal_from_session)):
    require_admin(p)
    with SessionLocal() as db:
        failed = db.scalars(select(Document).where(Document.tenant_id == p.tenant_id, Document.status == "failed").order_by(Document.created_at.desc()).limit(10)).all()
        return {"items": [{"type": "error", "title": f"文档处理失败：{doc.title}", "description": doc.error or "未记录错误", "resource_id": doc.id} for doc in failed]}


@app.get("/api/v1/admin/documents")
def admin_documents(p: Principal = Depends(principal_from_session),
                    offset: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=200)):
    require_admin(p)
    return _list_documents(p, offset, limit, administrative=True)


@app.get("/api/v1/admin/documents/{document_id}/grants")
def document_grants(document_id: str, p: Principal = Depends(principal_from_session)):
    require_admin(p)
    with SessionLocal() as db:
        document = db.scalar(select(Document).where(
            Document.id == document_id, Document.tenant_id == p.tenant_id,
        ))
        if document is None:
            raise HTTPException(404, "document not found")
        return {
            "document_id": document.id,
            "roles": sorted(_document_grant_roles(db, document.id, p.tenant_id)),
            "available_roles": db.scalars(select(Role.name).where(
                Role.tenant_id == p.tenant_id).order_by(Role.name)).all(),
        }


@app.put("/api/v1/admin/documents/{document_id}/grants")
def update_document_grants(document_id: str, body: DocumentGrantRequest,
                           p: Principal = Depends(principal_from_session)):
    require_admin(p)
    roles = _validated_roles(body.roles)
    with SessionLocal() as db:
        document = db.scalar(select(Document).where(
            Document.id == document_id, Document.tenant_id == p.tenant_id,
            Document.status.notin_(["deleted", "deleting"]),
        ).with_for_update())
        if document is None:
            raise HTTPException(404, "document not found")
        previous = _document_grant_roles(db, document.id, p.tenant_id)
        # Remove access in Qdrant before the database commits. A failed or
        # delayed expansion can deny access temporarily, but never leaks it.
        ready = document.status == "ready"
        if ready:
            try:
                _sync_document_acl(document, previous & roles)
            except Exception as exc:
                raise HTTPException(503, "vector authorization sync unavailable") from exc
        db.execute(delete(DocumentGrant).where(DocumentGrant.document_id == document.id))
        for name in roles:
            role = db.scalar(select(Role).where(Role.tenant_id == p.tenant_id, Role.name == name))
            if role is None:
                role = Role(id=str(uuid4()), tenant_id=p.tenant_id, name=name)
                db.add(role)
                db.flush()
            db.add(DocumentGrant(document_id=document.id, role_id=role.id))
        bump_knowledge_revision(p.tenant_id, db)
        audit(db, p, "document.grants.update", "document", document.id, "accepted")
        db.commit()
        if ready:
            try:
                _sync_document_acl(document, roles)
            except Exception as exc:
                raise HTTPException(503, "authorization saved; retry to finish vector sync") from exc
    return {"document_id": document_id, "roles": sorted(roles)}


@app.get("/api/v1/documents/{document_id}/content")
def document_content(document_id: str, p: Principal = Depends(principal_from_session)):
    with SessionLocal() as db:
        doc = db.scalar(select(Document).where(Document.id == document_id, *document_access(p)))
        if doc is None:
            raise HTTPException(404, "document not found")
        version = db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == doc.id,
                                                          DocumentVersion.version == doc.version))
        chunks = [] if version is None else db.scalars(
            select(Chunk).where(Chunk.document_version_id == version.id).order_by(Chunk.chunk_index)
        ).all()
        if chunks:
            content = "\n\n".join(chunk.content for chunk in chunks)[:20000]
        elif (doc.content_type or "").startswith("text/"):
            from app.storage import get_file
            content = get_file(doc.object_key).decode("utf-8", errors="replace")[:20000]
        else:
            content = "文档尚未完成解析；完成索引后将在这里显示可检索的正文。"
        return {"document_id": document_id, "title": doc.title, "version": doc.version,
                "chunk_count": len(chunks), "content": content}


def _persist_replace(document_id, p, filename, content_type, raw):
    with SessionLocal() as db:
        doc = db.scalar(select(Document).where(
            Document.id == document_id, Document.tenant_id == p.tenant_id
        ).with_for_update())
        if doc is None:
            raise HTTPException(404, "document not found")
        if doc.status in {"deleted", "deleting"}:
            raise HTTPException(409, "deleted document cannot be replaced")
        version = doc.version + 1
        key = f"{p.tenant_id}/documents/{doc.id}/versions/{version}/source/{filename}"
        current_versions = db.scalars(select(DocumentVersion).where(
            DocumentVersion.document_id == doc.id, DocumentVersion.status != "deleted"
        ).with_for_update()).all()
        new_version = DocumentVersion(id=str(uuid4()), document_id=doc.id, version=version,
                                      source_object_key=key, source_filename=filename,
                                      content_type=content_type, source_hash=sha256(raw).hexdigest(),
                                      created_by=p.subject, status="queued")
        doc.version, doc.object_key, doc.content_type, doc.status, doc.error = version, key, content_type, "queued", None
        db.add(new_version)
        db.flush()
        commit_started = False
        try:
            put_file(key, BytesIO(raw), content_type)
            for old_version in current_versions:
                if old_version.id == new_version.id:
                    continue
                old_version.status = "superseded"
                enqueue_document(db, doc, old_version, event_type="document.delete", trace_id=str(uuid4()))
            enqueue_document(db, doc, new_version, trace_id=str(uuid4()))
            audit(db, p, "document.replace", "document", doc.id, "accepted")
            bump_knowledge_revision(p.tenant_id, db)
            commit_started = True
            db.commit()
        except Exception as exc:
            db.rollback()
            if not commit_started:
                with suppress(Exception):
                    delete_file(key)
            raise HTTPException(503, "document storage unavailable") from exc
    return {"document_id": document_id, "version": version, "status": "queued"}


@app.put("/api/v1/documents/{document_id}", status_code=202)
async def replace_document(document_id: str, file: UploadFile = File(...), p: Principal = Depends(principal_from_session)):
    require_admin(p)
    filename, content_type = _upload_metadata(file)
    async with _UPLOAD_SLOTS:
        raw = await _read_document_upload(file, filename, content_type)
        return await run_sync(_persist_replace, document_id, p, filename, content_type, raw)


@app.get("/api/v1/admin/tasks")
def admin_tasks(p: Principal = Depends(principal_from_session)):
    require_admin(p)
    with SessionLocal() as db:
        rows = db.scalars(select(Document).where(Document.tenant_id == p.tenant_id).order_by(Document.created_at.desc())).all()
        return {"items": [{"id": x.id, "title": x.title, "stage": "indexing", "status": x.status,
                            "error": x.error, "updated_at": x.created_at.isoformat()} for x in rows]}


@app.get("/api/v1/admin/logs")
def admin_logs(p: Principal = Depends(principal_from_session)):
    require_admin(p)
    with SessionLocal() as db:
        rows = db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == p.tenant_id)
                          .order_by(AuditEvent.created_at.desc()).limit(200)).all()
        return {"items": [{"id": x.id, "action": x.action, "resource_type": x.resource_type,
                            "resource_id": x.resource_id, "outcome": x.outcome,
                            "subject": x.subject, "created_at": x.created_at.isoformat()} for x in rows]}


@app.get("/api/v1/admin/relations")
def admin_relations(query: str = "", p: Principal = Depends(principal_from_session)):
    require_admin(p)
    with SessionLocal() as db:
        statement = select(KnowledgeRelation, Document.title, DocumentVersion.version).join(
            DocumentVersion, DocumentVersion.id == KnowledgeRelation.document_version_id
        ).join(Document, Document.id == DocumentVersion.document_id).where(
            KnowledgeRelation.tenant_id == p.tenant_id, *document_access(p),
            Document.status == "ready", DocumentVersion.status == "ready",
            Document.version == DocumentVersion.version,
        )
        value = query.strip()
        if value:
            statement = statement.where((KnowledgeRelation.source.ilike(f"%{value}%")) |
                                        (KnowledgeRelation.target.ilike(f"%{value}%")) |
                                        (KnowledgeRelation.relation.ilike(f"%{value}%")))
        rows = db.execute(statement.order_by(KnowledgeRelation.created_at.desc()).limit(200)).all()
        return {"items": [{"id": relation.id, "source": relation.source, "target": relation.target,
                            "relation": relation.relation, "excerpt": relation.excerpt,
                            "document": title, "document_version": version,
                            "chunk_index": relation.chunk_index,
                            "created_at": relation.created_at.isoformat()}
                           for relation, title, version in rows]}


@app.get("/api/v1/admin/entity-aliases")
def admin_entity_aliases(p: Principal = Depends(principal_from_session)):
    require_admin(p)
    with SessionLocal() as db:
        rows = db.execute(select(EntityAlias, Document.title, DocumentVersion.version).join(
            DocumentVersion, DocumentVersion.id == EntityAlias.document_version_id
        ).join(Document, Document.id == DocumentVersion.document_id).where(
            EntityAlias.tenant_id == p.tenant_id, *document_access(p),
            Document.status == "ready", DocumentVersion.status == "ready",
            Document.version == DocumentVersion.version,
        ).order_by(EntityAlias.status, EntityAlias.created_at.desc()).limit(300)).all()
        return {"items": [{"id": item.id, "canonical": item.canonical, "alias": item.alias,
                            "status": item.status, "source": item.source, "excerpt": item.excerpt,
                            "chunk_index": item.chunk_index, "document": title,
                            "document_version": version, "created_at": item.created_at.isoformat()}
                           for item, title, version in rows]}


@app.post("/api/v1/admin/entity-aliases", status_code=201)
def create_entity_alias(body: EntityAliasRequest, p: Principal = Depends(principal_from_session)):
    require_admin(p)
    canonical, alias = body.canonical.strip(), body.alias.strip()
    if normalize_mention(canonical) == normalize_mention(alias):
        raise HTTPException(422, "canonical and alias must differ")
    with SessionLocal() as db:
        version = db.scalar(select(DocumentVersion).join(Document).where(
            Document.id == body.document_id, *document_access(p),
            Document.version == DocumentVersion.version, Document.status == "ready",
            DocumentVersion.status == "ready",
        ).with_for_update(of=Document))
        if version is None:
            raise HTTPException(404, "current document version not found")
        chunk = db.scalar(select(Chunk).where(
            Chunk.document_version_id == version.id, Chunk.chunk_index == body.chunk_index
        ))
        content = normalize_mention(chunk.content) if chunk else ""
        if not chunk or normalize_mention(canonical) not in content or normalize_mention(alias) not in content:
            raise HTTPException(422, "canonical and alias must co-occur in the cited chunk")
        item = db.scalar(select(EntityAlias).where(
            EntityAlias.document_version_id == version.id,
            EntityAlias.canonical == canonical, EntityAlias.alias == alias,
        ))
        if item is None:
            item = EntityAlias(tenant_id=p.tenant_id, document_version_id=version.id,
                               canonical=canonical, alias=alias, chunk_index=chunk.chunk_index,
                               excerpt=chunk.content[:600], source="admin", created_by=p.subject)
            db.add(item)
            db.flush()
            audit(db, p, "entity_alias.create", "entity_alias", item.id, "accepted")
            db.commit()
        return {"id": item.id, "status": item.status}


@app.put("/api/v1/admin/entity-aliases/{alias_id}")
def update_entity_alias(alias_id: str, body: EntityAliasStatusRequest,
                        p: Principal = Depends(principal_from_session)):
    require_admin(p)
    with SessionLocal() as db:
        version = db.scalar(select(DocumentVersion).join(Document).join(
            EntityAlias, EntityAlias.document_version_id == DocumentVersion.id
        ).where(EntityAlias.id == alias_id, *document_access(p),
                Document.version == DocumentVersion.version, Document.status == "ready",
                DocumentVersion.status == "ready").with_for_update(of=Document))
        if version is None:
            raise HTTPException(404, "current document version not found")
        item = db.scalar(select(EntityAlias).where(EntityAlias.id == alias_id, EntityAlias.tenant_id == p.tenant_id))
        if item is None:
            raise HTTPException(404, "entity alias not found")
        item.status = body.status
        item.approved_by = p.subject if body.status == "approved" else None
        item.approved_at = datetime.now(UTC).replace(tzinfo=None) if body.status == "approved" else None
        audit(db, p, f"entity_alias.{body.status}", "entity_alias", item.id, "accepted")
        bump_knowledge_revision(p.tenant_id, db)
        db.commit()
    return {"id": alias_id, "status": body.status}
