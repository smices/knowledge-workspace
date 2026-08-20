import jwt
import pytest
from fastapi import HTTPException
from app.auth import principal_from_token
from app.config import settings


@pytest.fixture(autouse=True)
def secure_test_secret(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", "test-secret-with-at-least-32-characters")


def token(**claims):
    return "Bearer " + jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def test_valid_principal_contains_tenant_and_roles():
    principal = principal_from_token(token(sub="u1", tenant_id="t1", roles=["finance"]))
    assert principal.subject == "u1"
    assert principal.tenant_id == "t1"
    assert principal.roles == {"finance"}


def test_missing_bearer_is_rejected():
    with pytest.raises(HTTPException) as error:
        principal_from_token(None)
    assert error.value.status_code == 401


def test_invalid_signature_is_rejected():
    bad = "Bearer " + jwt.encode({"sub": "u1", "tenant_id": "t1"}, "wrong-secret-012345678901234567890123", algorithm="HS256")
    with pytest.raises(HTTPException) as error:
        principal_from_token(bad)
    assert error.value.status_code == 401
