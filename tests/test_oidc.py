from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.auth import (
    OIDCDiscovery,
    _principal_from_claims,
    _sign_session,
    _verify_session,
    authorization_request,
    identity_logout_url,
    safe_next_path,
)
from app.config import settings
from app.main import app


def test_authorization_request_uses_pkce_and_nonce():
    metadata = OIDCDiscovery(
        authorization_endpoint="https://idp.example.test/authorize",
        token_endpoint="https://idp.example.test/token",
        jwks_uri="https://idp.example.test/jwks",
    )
    url, state, nonce, verifier = authorization_request(metadata)
    query = parse_qs(urlparse(url).query)
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [state]
    assert query["nonce"] == [nonce]
    assert query["code_challenge"]
    assert verifier
    assert query["redirect_uri"] == [settings.identity_redirect_uri]


def test_session_cookie_is_signed_and_contains_no_provider_token(monkeypatch):
    monkeypatch.setattr(settings, "identity_session_secret", "s" * 40)
    monkeypatch.setattr(settings, "identity_session_max_seconds", 300)
    principal = _principal_from_claims({"sub": "user-1", "tenant_id": "tenant-1", "roles": ["admin"]})
    cookie = _sign_session(principal, now=1_000)
    assert "access_token" not in cookie
    assert "refresh_token" not in cookie
    monkeypatch.setattr("app.auth.time.time", lambda: 1_100)
    assert _verify_session(cookie) == principal.__class__("user-1", "tenant-1", frozenset())


def test_oidc_claim_roles_are_not_application_roles():
    principal = _principal_from_claims(
        {"sub": "user-1", "tenant_id": "tenant-1", "roles": ["admin"], "realm_access": {"roles": ["admin"]}}
    )
    assert principal.roles == frozenset()


def test_oidc_claim_requires_tenant_context(monkeypatch):
    monkeypatch.setattr(settings, "identity_default_tenant_id", None)
    with pytest.raises(ValueError):
        _principal_from_claims({"sub": "user-1"})


def test_next_path_rejects_external_redirects():
    assert safe_next_path("/documents") == "/documents"
    assert safe_next_path("https://evil.example/") == "/home"
    assert safe_next_path("//evil.example/") == "/home"


def test_logout_uses_discovered_end_session_endpoint(monkeypatch):
    metadata = OIDCDiscovery(
        authorization_endpoint="https://idp.example.test/authorize",
        token_endpoint="https://idp.example.test/token",
        jwks_uri="https://idp.example.test/jwks",
        end_session_endpoint="https://idp.example.test/logout",
    )
    monkeypatch.setattr("app.auth.discover", lambda: metadata)
    monkeypatch.setattr(settings, "identity_post_logout_redirect_uri", "https://app.example.test/")
    url = identity_logout_url()
    assert url.startswith("https://idp.example.test/logout?")
    assert "client_id=sn-knowledge" in url
    assert "post_logout_redirect_uri=https%3A%2F%2Fapp.example.test%2F" in url


def test_logout_clears_local_session_when_provider_is_unavailable(monkeypatch):
    from app import main

    monkeypatch.setattr(settings, "auth_mode", "oidc")
    monkeypatch.setattr(main, "identity_logout_url", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    response = TestClient(app).get("/auth/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert settings.identity_session_cookie in response.headers.get("set-cookie", "")


def test_login_sets_one_time_oidc_cookies(monkeypatch):
    from app import main

    metadata = OIDCDiscovery(
        authorization_endpoint="https://idp.example.test/authorize",
        token_endpoint="https://idp.example.test/token",
        jwks_uri="https://idp.example.test/jwks",
    )
    monkeypatch.setattr(settings, "auth_mode", "oidc")
    monkeypatch.setattr(main, "authorization_request", lambda: authorization_request(metadata))
    client = TestClient(app)
    response = client.get("/auth/login?next=/documents", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("https://idp.example.test/authorize?")
    assert "oi_oidc_state" in response.headers.get("set-cookie", "")
    assert "oi_oidc_verifier" in response.headers.get("set-cookie", "")
