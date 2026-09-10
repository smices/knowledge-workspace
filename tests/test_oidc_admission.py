"""HTTP callback admission tests; the external IdP exchange is explicitly stubbed."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import db, main
from app.auth import Principal
from app.config import settings


@pytest.mark.parametrize("status,expected", [("active", 303), ("disabled", 403), (None, 403)])
def test_callback_requires_pre_admission_and_local_mode_rejects_idp_cookie(monkeypatch, status, expected):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", session)
    monkeypatch.setattr(main, "SessionLocal", session)
    monkeypatch.setattr(settings, "auth_mode", "oidc")
    monkeypatch.setattr(settings, "identity_default_tenant_id", "callback-tenant")
    monkeypatch.setattr(settings, "identity_session_secret", "synthetic-callback-session-secret-32")
    monkeypatch.setattr(settings, "identity_cookie_secure", False)
    with session() as store:
        store.add(db.Tenant(id="callback-tenant", name="Synthetic callback tenant"))
        if status:
            store.add(db.Principal(id="approved-sub", tenant_id="callback-tenant",
                                   principal_type="idp", status=status, session_version=3))
        store.commit()
    calls = []

    def exchange(code, verifier, nonce):
        calls.append((code, verifier, nonce))
        return Principal("approved-sub", "callback-tenant", frozenset())

    monkeypatch.setattr(main, "exchange_code", exchange)
    try:
        client = TestClient(main.app, base_url="http://testserver", follow_redirects=False)
        client.cookies.set("oi_oidc_state", "expected-state")
        assert client.get("/auth/callback?code=code&state=wrong-state").status_code == 400
        assert client.get("/auth/callback?code=code&state=expected-state").status_code == 400
        assert calls == []
        client.cookies.set("oi_oidc_nonce", "expected-nonce")
        client.cookies.set("oi_oidc_verifier", "expected-verifier")
        client.cookies.set("oi_oidc_next", "//external.invalid")
        response = client.get("/auth/callback?code=code&state=expected-state")
        assert response.status_code == expected
        assert calls == [("code", "expected-verifier", "expected-nonce")]
        with session() as store:
            assert store.query(db.Principal).count() == (1 if status else 0)
        if status != "active":
            assert settings.identity_session_cookie not in response.cookies
            return
        assert response.headers["location"] == "/home"
        assert client.get("/api/v1/account").status_code == 200
        monkeypatch.setattr(settings, "auth_mode", "local")
        assert client.get("/api/v1/account").status_code == 403
        assert client.get("/home").status_code == 303
        assert client.get("/auth/callback?code=unused&state=expected-state").status_code == 404
        assert len(calls) == 1
    finally:
        engine.dispose()
