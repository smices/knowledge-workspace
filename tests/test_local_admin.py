from fastapi.testclient import TestClient
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import auth, db, local_admin, main
from app.auth import Principal, _sign_session
from app.config import settings


def test_initial_local_admin_manages_idp_access_without_profile_state(monkeypatch):
    test_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.Base.metadata.create_all(test_engine)
    session = sessionmaker(bind=test_engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", session)
    monkeypatch.setattr(local_admin, "SessionLocal", session)
    monkeypatch.setattr(main, "SessionLocal", session)
    monkeypatch.setattr(settings, "auth_mode", "oidc")
    monkeypatch.setattr(settings, "identity_default_tenant_id", "tenant-1")
    monkeypatch.setattr(settings, "identity_session_secret", "s" * 40)
    monkeypatch.setattr(settings, "local_admin_username", "installer")
    monkeypatch.setattr(settings, "local_admin_password", "local-admin-password")

    local_admin.ensure_local_admin()
    monkeypatch.setattr(settings, "local_admin_password", "a-different-install-password")
    local_admin.ensure_local_admin()
    assert local_admin.authenticate_local_admin("installer", "local-admin-password")
    assert not local_admin.authenticate_local_admin("installer", "a-different-install-password")

    with session() as store:
        store.add(db.Principal(id="idp-user-1", tenant_id="tenant-1", display_name=None, principal_type="idp"))
        store.commit()

    client = TestClient(main.app)
    response = client.post("/auth/local-admin/login", data={"username": "installer", "password": "local-admin-password", "next": "/admin/"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/"
    assert client.get("/account").status_code == 404
    assert client.put("/api/v1/admin/members/idp-user-1/roles", json={"roles": ["finance"]}).json()["roles"] == ["finance"]
    assert client.put("/api/v1/admin/members/idp-user-1/status", json={"status": "disabled"}).json()["status"] == "disabled"
    assert client.put(f"/api/v1/admin/members/{local_admin.LOCAL_ADMIN_ID}/status", json={"status": "disabled"}).status_code == 400

    disabled = TestClient(main.app)
    disabled.cookies.set(settings.identity_session_cookie, _sign_session(Principal("idp-user-1", "tenant-1", frozenset())))
    assert disabled.get("/api/v1/documents").status_code == 403


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="requires isolated TEST_DATABASE_URL")
def test_concurrent_postgres_bootstrap_creates_one_admin(monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    url = make_url(os.environ["TEST_DATABASE_URL"])
    assert url.get_backend_name() == "postgresql"
    schema = "bootstrap_test_" + uuid4().hex
    control = create_engine(url)
    with control.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url.update_query_dict({"options": f"-csearch_path={schema}"}))
    try:
        db.Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine, expire_on_commit=False)
        monkeypatch.setattr(local_admin, "SessionLocal", session)
        monkeypatch.setattr(settings, "identity_default_tenant_id", "bootstrap-test")
        monkeypatch.setattr(settings, "local_admin_username", "test-installer")
        monkeypatch.setattr(settings, "local_admin_password", "synthetic-test-password")
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: local_admin.ensure_local_admin(), range(2)))
        with session() as store:
            assert store.query(db.LocalAdminCredential).count() == 1
            assert store.query(db.PrincipalRole).count() == 1
    finally:
        engine.dispose()
        with control.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        control.dispose()
