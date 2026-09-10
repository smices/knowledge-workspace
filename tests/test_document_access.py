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
                                 source_filename="sample.txt", content_type="text/plain", source_hash="synthetic", created_by="reader"))
        s.add(db.DocumentGrant(document_id="doc", role_id="hr-role"))
        s.add(db.Chunk(id="chunk", document_version_id="version", chunk_index=0,
                      content="synthetic restricted content", content_hash="synthetic"))
        s.commit()
    yield session
    main.app.dependency_overrides.pop(principal_from_session, None)
    engine.dispose()


def caller(tenant="tenant", roles=frozenset({"hr"})):
    main.app.dependency_overrides[principal_from_session] = lambda: Principal("reader", tenant, roles)
    return TestClient(main.app)


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


def test_pagination_bounds_and_admin_metadata_do_not_grant_content(store):
    client = caller(roles=frozenset({"admin"}))
    assert client.get("/api/v1/admin/documents?limit=1").json()["items"][0]["id"] == "doc"
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
