from pathlib import Path

import asyncio
import json
import re
from contextlib import asynccontextmanager, suppress
from hashlib import sha256
from io import BytesIO
from time import perf_counter
from uuid import uuid4

from fastapi import Depends, File, Form, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI, OpenAI
from qdrant_client import models
from redis import Redis
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.auth import (
    Principal,
    _sign_session,
    authorization_request,
    exchange_code,
    identity_account_url,
    identity_logout_url,
    principal_from_session,
    safe_next_path,
)
from app.config import settings
from app.db import (
    AuditEvent,
    Chunk,
    Document,
    DocumentGrant,
    DocumentVersion,
    KnowledgeBase,
    Principal as DbPrincipal,
    QueryEvent,
    Role,
    SessionLocal,
    Tenant,
    engine,
    init_db,
)
from app.events import publish_document
from app.storage import put_file
from app.vector import async_client as async_vector_client, client as vector_client, ensure_collection, search, search_async

llm = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
async_llm = AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
cache = Redis.from_url(settings.redis_url, decode_responses=True)

def cache_key(kind: str, *parts: object) -> str:
    return f"sn:{kind}:" + sha256("|".join(map(str, parts)).encode()).hexdigest()

def cache_get(key: str):
    try:
        value = cache.get(key)
        return json.loads(value) if value else None
    except Exception:
        return None

def cache_put(key: str, value: object, ttl: int):
    try:
        cache.setex(key, ttl, json.dumps(value, ensure_ascii=False))
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_collection()
    try:
        yield
    finally:
        await async_llm.close()
        await async_vector_client.close()


app = FastAPI(title=f"{settings.brand_name} API", version="0.2.0", lifespan=lifespan)


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


app.mount("/ui", StaticFiles(directory="web", html=True), name="ui")


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


class RoleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9:_-]+$")


def hmac_compare(left: str, right: str) -> bool:
    import hmac
    return bool(left) and hmac.compare_digest(left, right)


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
        role_id = f"{principal.tenant_id}:{role_name}"
        role = db.get(Role, role_id)
        if role is None:
            role = Role(id=role_id, tenant_id=principal.tenant_id, name=role_name)
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


def evidence_contract(answer: str, contexts: list[dict], state: str | None = None) -> list[dict]:
    state = state or answer_state(answer, contexts)
    conclusion = _conclusion_text(answer)
    claims = [re.sub(r"^[-*•]\s*", "", line).strip() for line in conclusion.splitlines() if line.strip()]
    if not claims and conclusion:
        claims = [conclusion]
    all_ids = _evidence_ids(answer, len(contexts))
    result = []
    for claim in claims:
        direct_ids = _evidence_ids(claim, len(contexts))
        ids = direct_ids or all_ids
        if state == "conflict":
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


def parse_relationships(raw: str, contexts: list[dict], query: str | None = None) -> list[dict]:
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
        if (not source or not target or source == target or len(source) > 32 or len(target) > 32
                or not label or len(label) > 24 or source not in content or target not in content
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
def root():
    return RedirectResponse("/ui/")


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


@app.get("/auth/login", include_in_schema=False)
def login(next: str = Query(default="/ui/")):
    authorization_url, state, nonce, verifier = authorization_request()
    response = RedirectResponse(authorization_url, status_code=303)
    cookie_kwargs = {"httponly": True, "secure": settings.identity_cookie_secure,
                     "samesite": "lax", "max_age": 300, "path": "/"}
    response.set_cookie("oi_oidc_state", state, **cookie_kwargs)
    response.set_cookie("oi_oidc_nonce", nonce, **cookie_kwargs)
    response.set_cookie("oi_oidc_verifier", verifier, **cookie_kwargs)
    response.set_cookie("oi_oidc_next", safe_next_path(next), **cookie_kwargs)
    return response


@app.get("/auth/callback", include_in_schema=False)
def callback(request, code: str | None = None, state: str | None = None,
             error: str | None = None, error_description: str | None = None):
    if error:
        raise HTTPException(401, error_description or "OIDC login denied")
    if not code or not state or not hmac_compare(state, request.cookies.get("oi_oidc_state", "")):
        raise HTTPException(400, "Invalid OIDC state")
    nonce, verifier = request.cookies.get("oi_oidc_nonce"), request.cookies.get("oi_oidc_verifier")
    if not nonce or not verifier:
        raise HTTPException(400, "OIDC login has expired")
    try:
        principal = exchange_code(code, verifier, nonce)
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
    provider_url = "/ui/"
    with suppress(Exception):
        provider_url = identity_logout_url()
    response = RedirectResponse(provider_url, status_code=303)
    response.delete_cookie(settings.identity_session_cookie, path="/")
    return response


@app.get("/account", include_in_schema=False)
def account():
    return RedirectResponse(identity_account_url(), status_code=303)


@app.post("/api/v1/documents", status_code=202)
async def upload_document(
    file: UploadFile = File(...),
    roles: str | None = Form(default=None),
    knowledge_base: str | None = Form(default=None),
    p: Principal = Depends(principal_from_session),
):
    if not file.filename:
        raise HTTPException(400, "filename required")
    filename = Path(file.filename.replace("\\", "/")).name
    if not filename:
        raise HTTPException(400, "filename required")
    raw = await file.read()
    if len(raw) > 100 * 1024 * 1024:
        raise HTTPException(413, "file exceeds 100MB limit")
    requested_roles = {x.strip() for x in (roles or "").split(",") if x.strip()}
    knowledge_base_name = (knowledge_base or "").strip()
    if len(knowledge_base_name) > 255:
        raise HTTPException(422, "knowledge base name exceeds 255 characters")
    if requested_roles and "admin" not in p.roles:
        raise HTTPException(403, "only admin can assign document roles")
    if knowledge_base_name and "admin" not in p.roles:
        raise HTTPException(403, "only admin can assign a knowledge base")
    effective_roles = requested_roles or set(p.roles)
    document_id = str(uuid4())
    version_id = str(uuid4())
    source_hash = sha256(raw).hexdigest()
    key = f"{p.tenant_id}/documents/{document_id}/versions/1/source/{filename}"
    duplicate_id = None
    duplicate_status = None
    with SessionLocal() as db:
        ensure_principal(db, p)
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
        duplicate = db.execute(select(Document.id, Document.status).join(DocumentVersion).where(
            Document.tenant_id == p.tenant_id,
            Document.knowledge_base_id == knowledge_base_id,
            DocumentVersion.source_hash == source_hash,
            Document.status != "deleted",
        )).first()
        if duplicate:
            duplicate_id, duplicate_status = duplicate
            if duplicate_status in {"queued", "failed"}:
                doc = db.get(Document, duplicate_id)
                doc.status, doc.error = "queued", None
                audit(db, p, "document.requeue", "document", duplicate_id, "accepted")
            db.commit()
        else:
            doc = Document(id=document_id, tenant_id=p.tenant_id, title=filename, status="queued",
                           created_by=p.subject, knowledge_base_id=knowledge_base_id, object_key=key,
                           content_type=file.content_type or "application/octet-stream")
            version = DocumentVersion(id=version_id, document_id=document_id, version=1,
                                     source_object_key=key, source_filename=filename,
                                     content_type=file.content_type or "application/octet-stream",
                                     source_hash=source_hash, created_by=p.subject)
            db.add_all([doc, version])
            db.flush()
            for role_name in effective_roles:
                role = db.scalar(select(Role).where(Role.tenant_id == p.tenant_id, Role.name == role_name))
                if role is None:
                    role = Role(id=f"{p.tenant_id}:{role_name}", tenant_id=p.tenant_id, name=role_name)
                    db.add(role)
                    db.flush()
                db.add(DocumentGrant(document_id=document_id, role_id=role.id))
            audit(db, p, "document.upload", "document", document_id, "accepted")
            db.commit()
    if duplicate_id:
        if duplicate_status in {"queued", "failed"}:
            await publish_document(duplicate_id)
            return {"document_id": duplicate_id, "status": "queued"}
        return {"document_id": duplicate_id, "status": "reused"}
    put_file(key, BytesIO(raw), file.content_type or "application/octet-stream")
    await publish_document(document_id)
    return {"document_id": document_id, "document_version_id": version_id, "status": "queued"}


@app.get("/api/v1/documents")
def list_documents(p: Principal = Depends(principal_from_session)):
    with SessionLocal() as db:
        rows = db.execute(select(Document, KnowledgeBase.name).outerjoin(
            KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id
        ).where(
            Document.tenant_id == p.tenant_id, Document.status != "deleted"
        ).order_by(Document.created_at.desc())).all()
        return {"items": [{"id": doc.id, "title": doc.title, "version": doc.version,
                            "knowledge_base": base_name, "content_type": doc.content_type,
                            "status": doc.status, "error": doc.error,
                            "created_at": doc.created_at.isoformat()}
                           for doc, base_name in rows]}


@app.get("/api/v1/documents/{document_id}/status")
def document_status(document_id: str, p: Principal = Depends(principal_from_session)):
    with SessionLocal() as db:
        doc = db.scalar(select(Document).where(Document.id == document_id, Document.tenant_id == p.tenant_id))
        if doc is None:
            raise HTTPException(404, "document not found")
        return {"id": doc.id, "title": doc.title, "status": doc.status, "error": doc.error}


@app.post("/api/v1/documents/{document_id}/reindex")
async def reindex_document(document_id: str, p: Principal = Depends(principal_from_session)):
    require_admin(p)
    with SessionLocal() as db:
        doc = db.scalar(select(Document).where(Document.id == document_id, Document.tenant_id == p.tenant_id))
        if doc is None:
            raise HTTPException(404, "document not found")
        doc.status, doc.error = "queued", None
        db.commit()
    await publish_document(document_id)
    return {"document_id": document_id, "status": "queued"}


@app.post("/api/v1/documents/{document_id}/cancel")
def cancel_document(document_id: str, p: Principal = Depends(principal_from_session)):
    require_admin(p)
    with SessionLocal() as db:
        doc = db.scalar(select(Document).where(Document.id == document_id, Document.tenant_id == p.tenant_id))
        if doc is None:
            raise HTTPException(404, "document not found")
        if doc.status in {"ready", "deleted", "canceled"}:
            raise HTTPException(409, f"cannot cancel document in status {doc.status}")
        doc.status, doc.error = "canceled", "任务由管理员取消"
        audit(db, p, "document.cancel", "document", document_id, "accepted")
        db.commit()
    return {"document_id": document_id, "status": "canceled"}


@app.delete("/api/v1/documents/{document_id}")
def delete_document(document_id: str, p: Principal = Depends(principal_from_session)):
    require_admin(p)
    with SessionLocal() as db:
        doc = db.scalar(select(Document).where(Document.id == document_id, Document.tenant_id == p.tenant_id))
        if doc is None:
            raise HTTPException(404, "document not found")
        vector_client.delete(
            settings.qdrant_collection,
            models.FilterSelector(filter=models.Filter(must=[
                models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id)),
            ])),
        )
        doc.status, doc.deleted_at = "deleted", func.now()
        audit(db, p, "document.delete", "document", document_id, "accepted")
        db.commit()
    return {"document_id": document_id, "status": "deleted"}


@app.post("/api/v1/retrieval/search")
@app.post("/search")
def retrieve(body: SearchRequest, p: Principal = Depends(principal_from_session)):
    started = perf_counter()
    key = cache_key("retrieval-v2", p.tenant_id, sorted(p.roles), body.query, body.limit, settings.embedding_model)
    cached = cache_get(key)
    if cached is not None:
        return cached
    if not p.roles:
        result = {"results": []}
        cache_put(key, result, 30)
        log_query(p, body.query, "retrieval", 0, started)
        return result
    vector = llm.embeddings.create(model=settings.embedding_model, input=body.query).data[0].embedding
    points = search(vector, p.tenant_id, set(p.roles), body.limit, body.query)
    result = {"results": [dict(point.payload or {}, score=point.score) for point in points]}
    cache_put(key, result, 120)
    log_query(p, body.query, "retrieval", len(points), started)
    return result


async def _answer(body: AnswerRequest, p: Principal):
    started = perf_counter()
    key = cache_key("answer-v2", p.tenant_id, sorted(p.roles), body.query, body.limit, body.temperature, settings.embedding_model, settings.chat_model)
    cached = cache_get(key)
    if cached is not None:
        return cached
    if not p.roles:
        log_query(p, body.query, "answer", 0, started)
        answer_text = "没有找到当前角色可访问的知识。"
        return {"answer": answer_text, "answer_state": "no_answer",
                "answer_state_label": ANSWER_STATE_LABELS["no_answer"],
                "evidence_contract": [], "citations": [], "graph_available": False}
    vector = (await async_llm.embeddings.create(
        model=settings.embedding_model, input=body.query
    )).data[0].embedding
    points = await search_async(vector, p.tenant_id, set(p.roles), min(body.limit, 3), body.query)
    contexts = [point.payload or {} for point in points]
    if not contexts:
        log_query(p, body.query, "answer", 0, started)
        answer_text = "根据当前可访问的知识，我无法确认这个问题。"
        return {"answer": answer_text, "answer_state": "no_answer",
                "answer_state_label": ANSWER_STATE_LABELS["no_answer"],
                "evidence_contract": [], "citations": [], "graph_available": False}
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
            {"role": "system", "content": "你是严格的检索问答助手。只回答问题本身，只能使用证据。禁止引入证据中没有的人物、事件或数字。不要把‘军师/谋士’推断成‘师徒/师生’，不要把合作关系改写成亲属关系。严格输出三段：\n## 结论\n## 归纳\n## 依据\n依据必须引用 [证据 1]、[证据 2]。无法确认就写‘资料不足’，不要编造。"},
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
    result = {"answer": answer_text, "answer_state": state,
              "answer_state_label": ANSWER_STATE_LABELS[state],
              "evidence_contract": evidence_contract(answer_text, contexts, state),
              "citations": citations, "graph_available": state == "answered"}
    cache_put(key, result, 300)
    log_query(p, body.query, "answer", len(citations), started)
    return result


async def _graph(body: GraphRequest, p: Principal):
    started = perf_counter()
    key = cache_key("graph-v2", p.tenant_id, sorted(p.roles), body.query, body.limit,
                    settings.embedding_model, settings.chat_model)
    cached = cache_get(key)
    if cached is not None:
        return cached
    if not p.roles:
        return {"nodes": [], "edges": [], "citations": [], "message": "没有可访问的知识。"}
    vector = (await async_llm.embeddings.create(model=settings.embedding_model, input=body.query)).data[0].embedding
    points = await search_async(vector, p.tenant_id, set(p.roles), min(body.limit, 3), body.query)
    contexts = [point.payload or {} for point in points]
    if not contexts:
        log_query(p, body.query, "graph", 0, started)
        return {"nodes": [], "edges": [], "citations": [], "message": "没有找到可建立关系的原文证据。"}
    evidence = "\n\n".join(
        f"[证据 {index + 1}] {item.get('title')}，第 {item.get('chunk_index')} 段\n{item.get('content', '')[:720]}"
        for index, item in enumerate(contexts)
    )
    completion = await async_llm.chat.completions.create(
        model=settings.chat_model,
        temperature=0,
        max_tokens=420,
        reasoning_effort="none",
        messages=[
            {"role": "system", "content": "你是原文证据关系抽取器。只提取问题点名实体之间、且由证据直接说明的关系，不返回其他实体对，绝不补充常识或推断。实体必须逐字出现在所引证据中。关系可以是技术、流程、组织或业务领域的任意简洁术语（例如 depends_on、owned_by、approved_by、triggers、blocks）；保留原文明确术语，不要强行套用小型固定词表。只输出 JSON 数组，最多 8 项，不要 Markdown：[{\"source\":\"实体甲\",\"target\":\"实体乙\",\"relation\":\"关系\",\"evidence\":1}]。evidence 是证据编号。"},
            {"role": "user", "content": f"问题：{body.query}\n\n证据：\n{evidence}\n\n只输出 JSON 数组，不要解释、标题或 Markdown。"},
        ],
    )
    edges = merge_relationships(parse_relationships(completion.choices[0].message.content or "", contexts, body.query))
    names = []
    for edge in edges:
        for name in (edge["source"], edge["target"]):
            if name not in names:
                names.append(name)
    result = {"nodes": [{"id": name, "label": name} for name in names], "edges": edges,
              "citations": citations_for(points, contexts),
              "message": "" if edges else "当前证据不足以确认人物或实体关系。"}
    cache_put(key, result, 300)
    log_query(p, body.query, "graph", len(edges), started)
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
    return await run_cancellable(_answer(body, p), request)


@app.post("/api/v1/rag/graph")
async def graph(body: GraphRequest, request: Request,
                p: Principal = Depends(principal_from_session)):
    return await run_cancellable(_graph(body, p), request)


@app.post("/api/v1/admin/roles")
def create_role(body: RoleRequest, p: Principal = Depends(principal_from_session)):
    require_admin(p)
    with SessionLocal() as db:
        ensure_principal(db, p)
        role = db.scalar(select(Role).where(Role.tenant_id == p.tenant_id, Role.name == body.name))
        if role is None:
            role = Role(id=f"{p.tenant_id}:{body.name}", tenant_id=p.tenant_id, name=body.name)
            db.add(role)
            db.commit()
        return {"id": role.id, "name": role.name}


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
                         .where(QueryEvent.tenant_id == p.tenant_id)
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
def admin_documents(p: Principal = Depends(principal_from_session)):
    require_admin(p)
    return list_documents(p)


@app.get("/api/v1/documents/{document_id}/content")
def document_content(document_id: str, p: Principal = Depends(principal_from_session)):
    with SessionLocal() as db:
        doc = db.scalar(select(Document).where(Document.id == document_id, Document.tenant_id == p.tenant_id))
        if doc is None:
            raise HTTPException(404, "document not found")
        version = db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == doc.id)
                            .order_by(DocumentVersion.version.desc()))
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


@app.put("/api/v1/documents/{document_id}", status_code=202)
async def replace_document(document_id: str, file: UploadFile = File(...), p: Principal = Depends(principal_from_session)):
    require_admin(p)
    if not file.filename:
        raise HTTPException(400, "filename required")
    filename = Path(file.filename.replace("\\", "/")).name
    if not filename:
        raise HTTPException(400, "filename required")
    raw = await file.read()
    if len(raw) > 100 * 1024 * 1024:
        raise HTTPException(413, "file exceeds 100MB limit")
    with SessionLocal() as db:
        doc = db.scalar(select(Document).where(Document.id == document_id, Document.tenant_id == p.tenant_id))
        if doc is None:
            raise HTTPException(404, "document not found")
        version = doc.version + 1
        key = f"{p.tenant_id}/documents/{doc.id}/versions/{version}/source/{filename}"
        doc.version, doc.object_key, doc.content_type, doc.status, doc.error = version, key, file.content_type, "queued", None
        db.add(DocumentVersion(id=str(uuid4()), document_id=doc.id, version=version,
                               source_object_key=key, source_filename=filename,
                               content_type=file.content_type or "application/octet-stream",
                               source_hash=sha256(raw).hexdigest(), created_by=p.subject))
        audit(db, p, "document.replace", "document", doc.id, "accepted")
        db.commit()
    put_file(key, BytesIO(raw), file.content_type or "application/octet-stream")
    await publish_document(document_id)
    return {"document_id": document_id, "version": version, "status": "queued"}


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
