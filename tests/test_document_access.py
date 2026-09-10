import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import db, main
from app.auth import Principal, principal_from_session


@pytest.fixture
def store(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", session)
    with session() as s:
        s.add(db.Tenant(id="tenant", name="tenant"))
        s.add(db.Principal(id="reader", tenant_id="tenant"))
        s.add(db.Role(id="hr-role", tenant_id="tenant", name="hr"))
        s.add(db.Document(id="doc", tenant_id="tenant", title="restricted", created_by="reader", status="ready"))
        s.add(db.DocumentVersion(id="version", document_id="doc", version=1, source_object_key="synthetic",
                                 source_filename="sample.txt", content_type="text/plain", source_hash="synthetic",
                                 created_by="reader", status="ready"))
        s.add(db.DocumentGrant(document_id="doc", role_id="hr-role"))
        s.add(db.Chunk(id="chunk", document_version_id="version", chunk_index=0,
                      content="synthetic restricted content", content_hash="synthetic"))
        s.commit()
    yield session
    main.app.dependency_overrides.pop(principal_from_session, None)
    engine.dispose()


def caller(tenant="tenant", roles=frozenset({"hr"}), headers=None):
    main.app.dependency_overrides[principal_from_session] = lambda: Principal("reader", tenant, roles)
    return TestClient(main.app, headers={"Origin": "http://testserver"} if headers is None else headers)


@pytest.mark.parametrize("tenant,roles", [("other", {"hr"}), ("tenant", {"finance"}), ("tenant", set())])
def test_document_routes_deny_ungranted_callers(store, tenant, roles):
    client = caller(tenant, frozenset(roles))
    assert client.get("/api/v1/documents").json()["items"] == []
    for suffix in ("status", "content"):
        assert client.get(f"/api/v1/documents/doc/{suffix}").status_code == 404


def test_role_union_revocation_and_deletion(store):
    client = caller(roles=frozenset({"finance", "hr"}))
    assert client.get("/api/v1/documents/doc/content").json()["content"] == "synthetic restricted content"
    assert len(client.get("/api/v1/documents").json()["items"]) == 1
    with store() as s:
        s.query(db.DocumentGrant).delete()
        s.commit()
    assert client.get("/api/v1/documents/doc/content").status_code == 404
    with store() as s:
        s.add(db.DocumentGrant(document_id="doc", role_id="hr-role"))
        s.get(db.Document, "doc").status = "deleted"
        s.commit()
    assert client.get("/api/v1/documents/doc/content").status_code == 404
    assert client.get("/api/v1/documents/doc/status").status_code == 404
    assert client.get("/api/v1/documents").json()["items"] == []


def test_document_grant_management_syncs_qdrant_and_denies_cross_tenant(store, monkeypatch):
    class Vector:
        def __init__(self):
            self.calls, self.fail, self.fail_on, self.attempts = [], False, None, 0

        def set_payload(self, _collection, payload, _selector, **_kwargs):
            self.attempts += 1
            if self.fail or self.fail_on == self.attempts:
                raise RuntimeError("qdrant unavailable")
            self.calls.append(payload)

    vector = Vector()
    monkeypatch.setattr(main, "vector_client", vector)
    admin = caller(roles=frozenset({"admin"}))
    assert admin.get("/api/v1/admin/documents/doc/grants").json()["roles"] == ["hr"]
    changed = admin.put("/api/v1/admin/documents/doc/grants", json={"roles": ["finance"]})
    assert changed.status_code == 200
    assert changed.json()["roles"] == ["finance"]
    assert vector.calls == [{"allowed_roles": []}, {"allowed_roles": ["finance"]}]
    with store() as s:
        assert main._document_grant_roles(s, "doc", "tenant") == {"finance"}
        assert s.get(db.Tenant, "tenant").knowledge_revision == 1
        assert s.query(db.AuditEvent).filter_by(action="document.grants.update").count() == 1
    assert caller(roles=frozenset({"finance"})).get("/api/v1/documents/doc/content").status_code == 200
    assert caller(roles=frozenset({"hr"})).get("/api/v1/documents/doc/content").status_code == 404

    vector.fail = True
    admin = caller(roles=frozenset({"admin"}))
    failed = admin.put("/api/v1/admin/documents/doc/grants", json={"roles": []})
    assert failed.status_code == 503
    with store() as s:
        assert main._document_grant_roles(s, "doc", "tenant") == {"finance"}
    assert caller("other", frozenset({"admin"})).get("/api/v1/admin/documents/doc/grants").status_code == 404

    vector.fail = False
    vector.fail_on = vector.attempts + 2
    admin = caller(roles=frozenset({"admin"}))
    delayed = admin.put("/api/v1/admin/documents/doc/grants", json={"roles": ["hr"]})
    assert delayed.status_code == 503
    with store() as s:
        assert main._document_grant_roles(s, "doc", "tenant") == {"hr"}
    assert caller(roles=frozenset({"finance"})).get("/api/v1/documents/doc/content").status_code == 404
    vector.fail_on = None
    admin = caller(roles=frozenset({"admin"}))
    assert admin.put("/api/v1/admin/documents/doc/grants", json={"roles": ["hr"]}).status_code == 200


def test_write_origin_gate_rejects_browser_requests_but_preserves_bearer_api_calls(store, monkeypatch):
    class Vector:
        def set_payload(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(main, "vector_client", Vector())
    no_origin = caller(roles=frozenset({"admin"}), headers={})
    assert no_origin.put("/api/v1/admin/documents/doc/grants", json={"roles": ["finance"]}).status_code == 403
    bearer = caller(roles=frozenset({"admin"}), headers={"Authorization": "Bearer synthetic"})
    assert bearer.put("/api/v1/admin/documents/doc/grants", json={"roles": ["finance"]}).status_code == 200


def test_pagination_bounds_and_admin_metadata_do_not_grant_content(store):
    client = caller(roles=frozenset({"admin"}))
    assert client.get("/api/v1/admin/documents?limit=1").json()["items"][0]["id"] == "doc"
    assert client.get("/api/v1/admin/documents?offset=1").json()["total"] == 1
    assert client.get("/api/v1/admin/documents?offset=1").json()["items"] == []
    assert client.get("/api/v1/documents/doc/content").status_code == 404
    assert client.get("/api/v1/documents?limit=201").status_code == 422
    assert client.get("/api/v1/documents?offset=-1").status_code == 422


def test_top_questions_does_not_expose_other_users_queries(store):
    with store() as s:
        s.add_all([db.QueryEvent(tenant_id="tenant", subject=subject, query=query,
                                endpoint="answer", result_count=0, duration_ms=1)
                   for subject, query in [("reader", "own question"), ("other", "private question")]])
        s.commit()
    assert caller().get("/api/v1/questions/top").json()["items"] == [{"question": "own question", "count": 1}]


def test_cached_citations_require_current_ready_and_authorized_version(store, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(main, "knowledge_revision", lambda _: 7)
    monkeypatch.setattr(main.settings, "auth_mode", "jwt")
    principal = Principal("reader", "tenant", frozenset({"hr"}))
    result = {"citations": [{"document_id": "doc", "document_version": 1}]}
    main.validate_query_result(principal, 7, result)
    main.validate_query_result(principal, 6, result)  # Unrelated ingestion does not invalidate live evidence.
    with pytest.raises(HTTPException, match="Evidence access changed"):
        main.validate_query_result(Principal("reader", "tenant", frozenset({"finance"})), 7, result)
    with store() as s:
        s.get(db.Document, "doc").version = 2
        s.commit()
    with pytest.raises(HTTPException, match="Evidence access changed"):
        main.validate_query_result(principal, 7, result)


def test_late_graph_cannot_recreate_deleted_relationships(store):
    with store() as s:
        s.get(db.Document, "doc").status = "deleted"
        s.commit()
    main.persist_relationships(Principal("reader", "tenant", frozenset({"hr"})),
                              [{"document_id": "doc", "document_version": 1, "chunk_index": 0}],
                              [{"source": "a", "target": "b", "label": "links", "excerpt": "synthetic", "evidence": 1}])
    with store() as s:
        assert s.query(db.KnowledgeRelation).count() == 0


@pytest.mark.parametrize("change", ["rejected", "removed"])
def test_unrelated_revision_is_allowed_but_withdrawn_alias_evidence_is_not(store, monkeypatch, change):
    from fastapi import HTTPException

    monkeypatch.setattr(main, "knowledge_revision", lambda _: 9)
    monkeypatch.setattr(main.settings, "auth_mode", "jwt")
    with store() as s:
        s.add(db.EntityAlias(id="alias", tenant_id="tenant", document_version_id="version",
                             canonical="alpha", alias="beta", chunk_index=0, excerpt="alpha beta", status="approved"))
        s.commit()
    principal = Principal("reader", "tenant", frozenset({"hr"}))
    result = {"citations": [{"document_id": "doc", "document_version": 1}],
              "entity_bindings": [{"alias_id": "alias", "evidence": [1]}]}
    main.validate_query_result(principal, 7, result)
    with store() as s:
        alias = s.get(db.EntityAlias, "alias")
        if change == "removed":
            s.delete(alias)
        else:
            alias.status = "rejected"
        s.commit()
    with pytest.raises(HTTPException, match="Alias evidence changed"):
        main.validate_query_result(principal, 7, result)


def test_revision_outage_does_not_reuse_an_unknown_cache_scope(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(main, "knowledge_revision", lambda _: None)
    with pytest.raises(HTTPException) as error:
        main.query_revision("tenant")
    assert error.value.status_code == 503


def test_alias_approval_and_revision_share_one_transaction(store, monkeypatch):
    with store() as s:
        s.add(db.EntityAlias(id="alias", tenant_id="tenant", document_version_id="version",
                             canonical="alpha", alias="beta", chunk_index=0, excerpt="alpha beta"))
        s.commit()
    principal = Principal("reader", "tenant", frozenset({"admin", "hr"}))
    main.update_entity_alias("alias", main.EntityAliasStatusRequest(status="approved"), principal)
    with store() as s:
        assert s.get(db.Tenant, "tenant").knowledge_revision == 1
        assert s.get(db.EntityAlias, "alias").status == "approved"
    def unavailable(*args):
        raise RuntimeError("revision unavailable")
    monkeypatch.setattr(main, "bump_knowledge_revision", unavailable)
    with pytest.raises(RuntimeError):
        main.update_entity_alias("alias", main.EntityAliasStatusRequest(status="rejected"), principal)
    with store() as s:
        assert s.get(db.EntityAlias, "alias").status == "approved"


def test_admin_evidence_routes_require_content_grant_and_current_version(store):
    with store() as s:
        s.add(db.Document(id="11111111-1111-1111-1111-111111111111", tenant_id="tenant",
                          title="restricted creation", created_by="reader", status="ready"))
        s.add(db.DocumentVersion(id="creation-version", document_id="11111111-1111-1111-1111-111111111111",
                                 version=1, source_object_key="synthetic", source_filename="sample.txt",
                                 content_type="text/plain", source_hash="synthetic", created_by="reader", status="ready"))
        s.add(db.DocumentGrant(document_id="11111111-1111-1111-1111-111111111111", role_id="hr-role"))
        s.add(db.Chunk(document_version_id="creation-version", chunk_index=0,
                       content="alpha beta", content_hash="synthetic"))
        s.add(db.EntityAlias(id="alias", tenant_id="tenant", document_version_id="version",
                             canonical="alpha", alias="beta", chunk_index=0, excerpt="private excerpt"))
        s.add(db.KnowledgeRelation(tenant_id="tenant", document_version_id="version", chunk_index=0,
                                   source="alpha", target="beta", relation="links", excerpt="private excerpt"))
        s.commit()
    client = caller(roles=frozenset({"admin"}))
    for path in ("relations", "entity-aliases"):
        assert client.get(f"/api/v1/admin/{path}").json()["items"] == []
    assert client.put("/api/v1/admin/entity-aliases/alias", json={"status": "approved"}).status_code == 404
    assert client.post("/api/v1/admin/entity-aliases", json={"document_id": "11111111-1111-1111-1111-111111111111", "chunk_index": 0,
                                                           "canonical": "alpha", "alias": "beta"}).status_code == 404
    client = caller(roles=frozenset({"admin", "hr"}))
    for path in ("relations", "entity-aliases"):
        assert len(client.get(f"/api/v1/admin/{path}").json()["items"]) == 1
    with store() as s:
        s.get(db.Document, "doc").version = 2
        s.commit()
    for path in ("relations", "entity-aliases"):
        assert client.get(f"/api/v1/admin/{path}").json()["items"] == []


def test_delete_retry_requeues_exhausted_cleanup_without_stealing_active_work(store):
    principal = Principal("reader", "tenant", frozenset({"admin"}))
    assert main.delete_document("doc", principal)["status"] == "deleting"
    with store() as s:
        job = s.query(db.IngestionJob).filter_by(stage="delete").one()
        job.status = "processing"
        s.commit()
    assert main.delete_document("doc", principal)["status"] == "deleting"
    with store() as s:
        job = s.query(db.IngestionJob).one()
        assert job.generation == 1
        assert s.query(db.EventOutbox).count() == 1
        job.status, job.attempt = "dead_letter", 3
        s.get(db.DocumentVersion, "version").status = "failed"
        s.commit()
    assert main.delete_document("doc", principal)["status"] == "deleting"
    with store() as s:
        job = s.query(db.IngestionJob).one()
        assert (job.status, job.attempt, job.generation) == ("queued", 0, 2)
        assert s.query(db.EventOutbox).count() == 2
