import base64
from time import time

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import pytest
from fastapi import HTTPException
from jwt import PyJWKClient as RealPyJWKClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app import auth, db
from app.auth import (OIDCDiscovery, Principal, _sign_session, authorization_request,
                      principal_from_oidc_token, principal_from_session)
from app.config import settings


class ServiceStore:
    def __init__(self, session, private_key):
        self.session = session
        self.private_key = private_key

    def __call__(self):
        return self.session()


def _base64_uint(value: int) -> str:
    size = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(size, "big")).rstrip(b"=").decode()


def _rsa_jwk(private_key, kid: str) -> dict[str, str]:
    numbers = private_key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "n": _base64_uint(numbers.n),
        "e": _base64_uint(numbers.e),
        "alg": "RS256",
        "kid": kid,
        "use": "sig",
    }


@pytest.fixture
def service_store(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", session)
    monkeypatch.setattr(settings, "auth_mode", "oidc")
    monkeypatch.setattr(settings, "jwt_secret", "test-secret-with-at-least-32-characters")
    monkeypatch.setattr(settings, "identity_issuer", "https://idp.example.test/realms/openidentity")
    monkeypatch.setattr(settings, "identity_client_id", "sn-knowledge")
    monkeypatch.setattr(settings, "identity_default_tenant_id", "tenant-1")
    with session() as store:
        store.add(db.Tenant(id="tenant-1", name="Tenant 1"))
        store.add(db.Principal(id="service-1", tenant_id="tenant-1", display_name="Worker",
                               principal_type="service_account"))
        role = db.Role(id="tenant-1:reader", tenant_id="tenant-1", name="reader")
        store.add(role)
        store.add(db.PrincipalRole(principal_id="service-1", role_id=role.id))
        store.commit()

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = _rsa_jwk(private_key, "service-key")
    metadata = OIDCDiscovery(
        authorization_endpoint="https://idp.example.test/authorize",
        token_endpoint="https://idp.example.test/token",
        jwks_uri="https://idp.example.test/jwks",
    )
    monkeypatch.setattr(auth, "discover", lambda: metadata)
    auth._jwks_client.cache_clear()

    class MockJWKClient:
        def __init__(self, uri, *, timeout, cache_jwk_set, lifespan):
            assert uri == metadata.jwks_uri
            assert timeout == settings.identity_http_timeout_seconds
            assert cache_jwk_set is True
            assert lifespan == 300

        def get_signing_key_from_jwt(self, token):
            return jwt.PyJWK.from_dict(jwk)

    monkeypatch.setattr(auth.jwt, "PyJWKClient", MockJWKClient)
    yield ServiceStore(session, private_key)
    auth._jwks_client.cache_clear()
    engine.dispose()


def bearer(private_key, **overrides):
    without_exp = overrides.pop("without_exp", False)
    claims = {
        "iss": settings.identity_issuer,
        "aud": settings.identity_client_id,
        "sub": "service-1",
        "tenant_id": "tenant-1",
    }
    if not without_exp:
        claims["exp"] = int(time()) + 300
    claims.update(overrides)
    return "Bearer " + jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "service-key"})


def test_valid_service_principal_uses_database_roles_and_ignores_token_roles(service_store):
    principal = principal_from_oidc_token(bearer(service_store.private_key, roles=["admin"]))
    assert principal.subject == "service-1"
    assert principal.tenant_id == "tenant-1"
    assert principal.roles == {"reader"}
    assert principal.source == "service"


@pytest.mark.parametrize("claim", [{"iss": "https://other.example/"}, {"aud": "other-client"}])
def test_invalid_issuer_or_audience_is_rejected(service_store, claim):
    with pytest.raises(HTTPException) as error:
        principal_from_oidc_token(bearer(service_store.private_key, **claim))
    assert error.value.status_code == 401


def test_invalid_signature_is_rejected(service_store):
    claims = {
        "iss": settings.identity_issuer,
        "aud": settings.identity_client_id,
        "sub": "service-1",
        "tenant_id": "tenant-1",
        "exp": int(time()) + 300,
    }
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    invalid = "Bearer " + jwt.encode(claims, wrong_key, algorithm="RS256", headers={"kid": "service-key"})
    with pytest.raises(HTTPException) as error:
        principal_from_oidc_token(invalid)
    assert error.value.status_code == 401


def test_missing_or_expired_expiry_is_rejected(service_store):
    with pytest.raises(HTTPException) as missing:
        principal_from_oidc_token(bearer(service_store.private_key, without_exp=True))
    assert missing.value.status_code == 401

    with pytest.raises(HTTPException) as expired:
        principal_from_oidc_token(bearer(service_store.private_key, exp=int(time()) - 1))
    assert expired.value.status_code == 401


def test_cross_tenant_and_disabled_service_principals_are_rejected(service_store):
    with pytest.raises(HTTPException) as cross_tenant:
        principal_from_oidc_token(bearer(service_store.private_key, tenant_id="tenant-2"))
    assert cross_tenant.value.status_code == 401

    with service_store() as store:
        store.get(db.Principal, "service-1").status = "disabled"
        store.commit()
    with pytest.raises(HTTPException) as disabled:
        principal_from_oidc_token(bearer(service_store.private_key))
    assert disabled.value.status_code == 403


def test_disabled_tenant_rejects_browser_session_provisioning(service_store, monkeypatch):
    with service_store() as store:
        store.get(db.Tenant, "tenant-1").status = "disabled"
        store.commit()
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/api/v1/rag/answer",
        "headers": [(b"cookie", f"sn_knowledge_session={_sign_session(Principal('user-1', 'tenant-1', frozenset()))}".encode())],
    })
    with pytest.raises(HTTPException) as error:
        principal_from_session(request, None)
    assert error.value.status_code == 403


def test_local_admin_session_requires_existing_local_identity_and_revalidates(service_store):
    def request_for(subject="local-admin"):
        return Request({
            "type": "http",
            "method": "GET",
            "path": "/api/v1/rag/answer",
            "headers": [(b"cookie", f"sn_knowledge_session={_sign_session(Principal(subject, 'tenant-1', frozenset(), source='local'))}".encode())],
        })

    with pytest.raises(HTTPException) as missing:
        principal_from_session(request_for(), None)
    assert missing.value.status_code == 403

    with service_store() as store:
        store.add(db.Principal(id="local-admin", tenant_id="tenant-1",
                               display_name="Local administrator", principal_type="local_admin"))
        store.commit()
    principal = principal_from_session(request_for(), None)
    assert principal.subject == "local-admin"
    assert principal.source == "local"


def test_discovery_failure_is_unavailable_not_server_error(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "oidc")
    monkeypatch.setattr(auth, "discover", lambda: (_ for _ in ()).throw(RuntimeError("bad discovery")))
    with pytest.raises(HTTPException) as error:
        principal_from_oidc_token("Bearer malformed")
    assert error.value.status_code == 503
    with pytest.raises(HTTPException) as error:
        authorization_request()
    assert error.value.status_code == 503


def test_unknown_kid_refresh_is_cooled_down_per_jwks_uri(service_store, monkeypatch):
    auth._jwks_client.cache_clear()
    auth._JWKS_REFRESH_UNTIL.clear()
    calls = []

    class UnknownKidClient:
        def __init__(self, uri, *, timeout, cache_jwk_set, lifespan):
            self.uri = uri

        def get_signing_key_from_jwt(self, token):
            calls.append(token)
            raise jwt.PyJWKClientError("unknown kid")

    monkeypatch.setattr(auth.jwt, "PyJWKClient", UnknownKidClient)
    token = jwt.encode({"sub": "service-1"}, service_store.private_key,
                       algorithm="RS256", headers={"kid": "attacker-kid"})
    for _ in range(2):
        with pytest.raises(HTTPException) as error:
            principal_from_oidc_token("Bearer " + token)
        assert error.value.status_code == 401
    assert len(calls) == 1
    auth._JWKS_REFRESH_UNTIL.clear()
    auth._jwks_client.cache_clear()


def test_role_changes_apply_to_next_request(service_store):
    assert principal_from_oidc_token(bearer(service_store.private_key)).roles == {"reader"}
    with service_store() as store:
        store.query(db.PrincipalRole).filter_by(principal_id="service-1").delete()
        role = db.Role(id="tenant-1:writer", tenant_id="tenant-1", name="writer")
        store.add(role)
        store.add(db.PrincipalRole(principal_id="service-1", role_id=role.id))
        store.commit()
    assert principal_from_oidc_token(bearer(service_store.private_key)).roles == {"writer"}


def test_jwks_client_reuses_configured_timeout(service_store):
    principal_from_oidc_token(bearer(service_store.private_key))
    principal_from_oidc_token(bearer(service_store.private_key))
    cache = auth._jwks_client.cache_info()
    assert cache.misses == 1
    assert cache.hits == 1


def test_real_jwks_client_warm_cache_validates_without_refetch(service_store, monkeypatch):
    monkeypatch.setattr(auth.jwt, "PyJWKClient", RealPyJWKClient)
    auth._jwks_client.cache_clear()
    client = auth._jwks_client("https://idp.example.test/jwks", settings.identity_http_timeout_seconds)
    client.jwk_set_cache.put({"keys": [_rsa_jwk(service_store.private_key, "service-key")]})
    fetches = []
    monkeypatch.setattr(client, "fetch_data", lambda: (fetches.append(True) or {}))

    assert principal_from_oidc_token(bearer(service_store.private_key)).source == "service"
    assert principal_from_oidc_token(bearer(service_store.private_key)).source == "service"
    assert fetches == []
    auth._jwks_client.cache_clear()


def test_real_jwks_client_non_signing_and_unknown_kids_are_cooled_down(service_store, monkeypatch):
    monkeypatch.setattr(auth.jwt, "PyJWKClient", RealPyJWKClient)
    auth._jwks_client.cache_clear()
    auth._JWKS_REFRESH_UNTIL.clear()
    client = auth._jwks_client("https://idp.example.test/jwks", settings.identity_http_timeout_seconds)
    non_signing = _rsa_jwk(service_store.private_key, "encryption-key")
    non_signing["use"] = "enc"
    client.jwk_set_cache.put({"keys": [non_signing]})
    fetches = []
    monkeypatch.setattr(client, "fetch_data", lambda: (fetches.append(True) or {"keys": [non_signing]}))
    token = jwt.encode({"sub": "service-1"}, service_store.private_key,
                       algorithm="RS256", headers={"kid": "encryption-key"})
    for _ in range(2):
        with pytest.raises(HTTPException) as error:
            principal_from_oidc_token("Bearer " + token)
        assert error.value.status_code == 401
    assert fetches == []

    auth._JWKS_REFRESH_UNTIL.clear()
    signing = _rsa_jwk(service_store.private_key, "service-key")
    client.jwk_set_cache.put({"keys": [signing]})
    monkeypatch.setattr(client, "fetch_data", lambda: (fetches.append(True) or {"keys": [signing]}))
    unknown = jwt.encode({"sub": "service-1"}, service_store.private_key,
                         algorithm="RS256", headers={"kid": "unknown-key"})
    for _ in range(2):
        with pytest.raises(HTTPException) as error:
            principal_from_oidc_token("Bearer " + unknown)
        assert error.value.status_code == 401
    assert fetches == [True]
    auth._JWKS_REFRESH_UNTIL.clear()
    auth._jwks_client.cache_clear()


def test_jwks_client_refreshes_once_after_key_rotation(monkeypatch):
    auth._jwks_client.cache_clear()
    old_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    responses = iter([
        {"keys": [_rsa_jwk(old_key, "old-key")]},
        {"keys": [_rsa_jwk(new_key, "new-key")]},
    ])
    client = auth._jwks_client("https://idp.example.test/jwks", 8.0)
    fetches = []
    monkeypatch.setattr(client, "fetch_data", lambda: (fetches.append(True) or next(responses)))

    assert client.get_signing_key("old-key").key.public_numbers() == old_key.public_key().public_numbers()
    assert client.get_signing_key("new-key").key.public_numbers() == new_key.public_key().public_numbers()
    assert len(fetches) == 2
    auth._jwks_client.cache_clear()


def test_discovery_is_cached_and_rejects_untrusted_endpoints(monkeypatch):
    monkeypatch.setattr(settings, "identity_issuer", "https://idp.example.test/realms/openidentity")
    monkeypatch.setattr(settings, "identity_http_timeout_seconds", 8.0)
    auth._discover_cached.cache_clear()
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "issuer": settings.identity_issuer,
                "authorization_endpoint": "https://idp.example.test/authorize",
                "token_endpoint": "https://idp.example.test/token",
                "jwks_uri": "https://idp.example.test/jwks",
            }

    monkeypatch.setattr(auth.httpx, "get", lambda *args, **kwargs: (calls.append(kwargs) or Response()))
    assert auth.discover() == auth.discover()
    assert len(calls) == 1
    assert calls[0]["timeout"] == 8.0

    auth._discover_cached.cache_clear()
    monkeypatch.setattr(Response, "json", lambda self: {
        "issuer": settings.identity_issuer,
        "authorization_endpoint": "https://idp.example.test/authorize",
        "token_endpoint": "http://evil.example.test/token",
        "jwks_uri": "https://idp.example.test/jwks",
    })
    with pytest.raises(RuntimeError, match="insecure token endpoint"):
        auth.discover()
    auth._discover_cached.cache_clear()


def test_invalid_bearer_does_not_fall_back_to_browser_cookie(service_store):
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/api/v1/rag/answer",
        "headers": [(b"cookie", b"sn_knowledge_session=valid-cookie")],
    })
    with pytest.raises(HTTPException) as error:
        principal_from_session(request, "Bearer not-a-token")
    assert error.value.status_code == 401
