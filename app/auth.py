"""Authentication helpers for local bearer tokens and OpenIdentity OIDC.

The browser flow is deliberately server-side: tokens are exchanged and verified
by this service, while the browser receives only a signed, short-lived session
cookie containing the stable subject and app-local tenant context. Refresh
tokens and client secrets never enter a prompt, URL, or cookie.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import httpx
import jwt
from fastapi import Header, HTTPException, Request
from sqlalchemy import select

from app.config import settings


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    roles: frozenset[str]


@dataclass(frozen=True)
class OIDCDiscovery:
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str | None = None
    end_session_endpoint: str | None = None


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _issuer() -> str:
    value = settings.identity_issuer.rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("IDENTITY_ISSUER must use HTTPS outside local development")
    return value


def _session_secret() -> bytes:
    value = settings.identity_session_secret or settings.jwt_secret
    if len(value) < 32:
        raise RuntimeError("IDENTITY_SESSION_SECRET must contain at least 32 characters")
    return value.encode("utf-8")


def new_pkce_pair() -> tuple[str, str]:
    """Return a verifier and RFC 7636 S256 challenge."""
    verifier = _b64(secrets.token_bytes(32))
    challenge = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def authorization_request(metadata: OIDCDiscovery | None = None) -> tuple[str, str, str, str]:
    """Build an authorization URL and its one-time state, nonce, and verifier."""
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier, challenge = new_pkce_pair()
    query = urlencode(
        {
            "client_id": settings.identity_client_id,
            "redirect_uri": settings.identity_redirect_uri,
            "response_type": "code",
            "scope": settings.identity_scope,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    endpoint = (metadata or discover()).authorization_endpoint
    return f"{endpoint}?{query}", state, nonce, verifier


def safe_next_path(value: str | None) -> str:
    """Keep post-login navigation on this application origin."""
    if not value:
        return "/docs"
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return "/docs"
    return value


def _principal_from_claims(claims: dict[str, Any]) -> Principal:
    subject = claims.get("sub")
    tenant_id = claims.get("tenant_id") or settings.identity_default_tenant_id
    if not isinstance(subject, str) or not subject.strip() or not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("OIDC identity does not contain an application tenant")
    # IdP roles are informational only. Application roles must be assigned in
    # sn_knowledge's own database; accepting them from the token would create a
    # privilege-escalation side door.
    return Principal(subject=subject, tenant_id=tenant_id, roles=frozenset())


def _sign_session(principal: Principal, now: int | None = None) -> str:
    issued = int(time.time() if now is None else now)
    payload = {
        "sub": principal.subject,
        "tenant_id": principal.tenant_id,
        "roles": sorted(principal.roles),
        "iat": issued,
        "exp": issued + settings.identity_session_max_seconds,
    }
    encoded = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64(hmac.new(_session_secret(), encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def _verify_session(value: str) -> Principal:
    try:
        encoded, signature = value.split(".", 1)
        expected = _b64(hmac.new(_session_secret(), encoded.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid session signature")
        payload = json.loads(_unb64(encoded))
        if int(payload["exp"]) <= int(time.time()):
            raise ValueError("expired session")
        return Principal(
            subject=str(payload["sub"]),
            tenant_id=str(payload["tenant_id"]),
            roles=frozenset(str(role) for role in payload.get("roles", [])),
        )
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(401, "Invalid session") from exc


def principal_from_token(authorization: str | None = Header(default=None)) -> Principal:
    """Validate the legacy local bearer token used by API-only development."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Bearer token required")
    try:
        claims = jwt.decode(authorization[7:], settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        tenant_id, subject = claims["tenant_id"], claims["sub"]
        roles = frozenset(claims.get("roles", []))
        if not tenant_id or not subject:
            raise ValueError
        return Principal(subject, tenant_id, roles)
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(401, "Invalid token") from exc


def principal_from_session(
    request: Request, authorization: str | None = Header(default=None)
) -> Principal:
    if settings.auth_mode.lower() == "dev":
        # ponytail: local-only bypass; never enable AUTH_MODE=dev in IDC.
        return Principal("local-admin", "tenant-local", frozenset({"admin", "finance"}))
    session = request.cookies.get(settings.identity_session_cookie)
    if session:
        principal = _verify_session(session)
        # IdP identity is authentication only. Resolve business roles from the
        # application's own database so an IdP claim can never elevate access.
        try:
            from app.db import PrincipalRole, Role, SessionLocal

            with SessionLocal() as db:
                role_names = db.scalars(
                    select(Role.name)
                    .join(PrincipalRole, PrincipalRole.role_id == Role.id)
                    .where(
                        PrincipalRole.principal_id == principal.subject,
                        Role.tenant_id == principal.tenant_id,
                    )
                ).all()
            return Principal(principal.subject, principal.tenant_id, frozenset(role_names))
        except Exception as exc:
            # A database failure must not turn into an unauthenticated request.
            raise HTTPException(503, "Application authorization unavailable") from exc
    if settings.auth_mode.lower() == "jwt":
        return principal_from_token(authorization)
    raise HTTPException(401, "OIDC login required")


def discover() -> OIDCDiscovery:
    response = httpx.get(
        _issuer() + "/.well-known/openid-configuration",
        timeout=settings.identity_http_timeout_seconds,
        follow_redirects=False,
    )
    response.raise_for_status()
    data = response.json()
    try:
        return OIDCDiscovery(
            authorization_endpoint=str(data["authorization_endpoint"]),
            token_endpoint=str(data["token_endpoint"]),
            jwks_uri=str(data["jwks_uri"]),
            userinfo_endpoint=data.get("userinfo_endpoint"),
            end_session_endpoint=data.get("end_session_endpoint"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("OIDC discovery response is incomplete") from exc


def exchange_code(code: str, verifier: str, nonce: str) -> Principal:
    if not code or not verifier or not nonce:
        raise HTTPException(400, "Invalid OIDC callback")
    metadata = discover()
    response = httpx.post(
        metadata.token_endpoint,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.identity_redirect_uri,
            "client_id": settings.identity_client_id,
            "code_verifier": verifier,
            **({"client_secret": settings.identity_client_secret} if settings.identity_client_secret else {}),
        },
        timeout=settings.identity_http_timeout_seconds,
        follow_redirects=False,
    )
    response.raise_for_status()
    tokens = response.json()
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str):
        raise HTTPException(401, "OIDC provider did not return an ID token")
    try:
        key = jwt.PyJWKClient(metadata.jwks_uri).get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=["RS256", "ES256", "PS256"],
            audience=settings.identity_client_id,
            issuer=_issuer(),
            options={"require": ["exp", "iat", "iss", "sub", "aud", "nonce"]},
        )
        if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise ValueError("OIDC nonce mismatch")
        return _principal_from_claims(claims)
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise HTTPException(401, "Invalid OIDC identity") from exc


def identity_account_url() -> str:
    return urljoin(_issuer() + "/", "account/")


def identity_logout_url(metadata: OIDCDiscovery | None = None) -> str:
    """Build the provider logout URL from discovery, never a vendor path."""
    endpoint = (metadata or discover()).end_session_endpoint
    if not endpoint:
        raise RuntimeError("OpenIdentity discovery does not advertise end_session_endpoint")
    params = {"client_id": settings.identity_client_id}
    if settings.identity_post_logout_redirect_uri:
        params["post_logout_redirect_uri"] = settings.identity_post_logout_redirect_uri
    return f"{endpoint}?{urlencode(params)}"


__all__ = [
    "OIDCDiscovery",
    "Principal",
    "authorization_request",
    "discover",
    "exchange_code",
    "identity_account_url",
    "identity_logout_url",
    "new_pkce_pair",
    "principal_from_session",
    "principal_from_token",
    "safe_next_path",
    "_principal_from_claims",
    "_sign_session",
    "_verify_session",
]
