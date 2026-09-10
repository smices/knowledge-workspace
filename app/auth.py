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
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
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
    source: str = "idp"


@dataclass(frozen=True)
class OIDCDiscovery:
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str | None = None
    end_session_endpoint: str | None = None
    issuer: str | None = None


_DISCOVERY_CACHE_TTL_SECONDS = 300
_DISCOVERY_CACHE_SIZE = 16
_JWKS_CACHE_SIZE = 16
_JWKS_REFRESH_COOLDOWN_SECONDS = 5.0
_JWKS_REFRESH_MAX_ISSUERS = 16
_JWKS_REFRESH_LOCK = threading.Lock()
_JWKS_REFRESH_UNTIL: dict[str, float] = {}


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
    try:
        endpoint = (metadata or discover()).authorization_endpoint
    except (RuntimeError, httpx.HTTPError) as exc:
        raise HTTPException(503, "OIDC discovery unavailable") from exc
    return f"{endpoint}?{query}", state, nonce, verifier


def safe_next_path(value: str | None) -> str:
    """Keep post-login navigation on this application origin."""
    if not value:
        return "/home"
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return "/home"
    return value


def browser_session_active(request: Request) -> bool:
    """Return whether this browser has a valid application session cookie."""
    value = request.cookies.get(settings.identity_session_cookie)
    if not value:
        return False
    if settings.auth_mode.lower() == "dev":
        # ponytail: development-only marker; production always verifies a signed OIDC session.
        return hmac.compare_digest(value, "dev")
    if settings.auth_mode.lower() != "oidc":
        return False
    try:
        _verify_session(value)
    except HTTPException:
        return False
    return True


def _principal_from_claims(claims: dict[str, Any]) -> Principal:
    subject = claims.get("sub")
    claim_tenant = claims.get("tenant_id")
    configured_tenant = settings.identity_default_tenant_id
    if claim_tenant is not None and (
        not isinstance(claim_tenant, str) or not claim_tenant.strip()
    ):
        raise ValueError("OIDC identity contains an invalid application tenant")
    if configured_tenant and claim_tenant and claim_tenant != configured_tenant:
        raise ValueError("OIDC identity belongs to another application tenant")
    tenant_id = claim_tenant or configured_tenant
    if not isinstance(subject, str) or not subject.strip() or not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("OIDC identity does not contain an application tenant")
    # IdP roles are informational only. Application roles must be assigned in
    # sn_knowledge's own database; accepting them from the token would create a
    # privilege-escalation side door.
    return Principal(subject=subject, tenant_id=tenant_id, roles=frozenset())


def _database_principal(principal: Principal, *, provision: bool, service_only: bool = False) -> Principal:
    """Resolve current application access; bearer callers must already exist."""
    from app.db import Principal as DbPrincipal, PrincipalRole, Role, SessionLocal, Tenant

    try:
        with SessionLocal() as db:
            tenant = db.get(Tenant, principal.tenant_id)
            identity = db.get(DbPrincipal, principal.subject)
            if provision:
                if tenant is None:
                    tenant = Tenant(id=principal.tenant_id, name=principal.tenant_id)
                    db.add(tenant)
                elif tenant.status != "active":
                    raise HTTPException(403, "Application access is disabled")
                if identity is None:
                    if principal.source == "local":
                        raise HTTPException(403, "Application access is disabled")
                    identity = DbPrincipal(id=principal.subject, tenant_id=principal.tenant_id,
                                           display_name=None, principal_type="idp")
                    db.add(identity)
                elif (identity.tenant_id != principal.tenant_id or identity.status != "active"
                      or (identity.principal_type == "local_admin") != (principal.source == "local")):
                    raise HTTPException(403, "Application access is disabled")
                db.commit()
            elif (tenant is None or tenant.status != "active" or identity is None
                  or identity.tenant_id != principal.tenant_id or identity.status != "active"
                  or (identity.principal_type == "local_admin") != (principal.source == "local")
                  or (service_only and identity.principal_type != "service_account")):
                raise HTTPException(403, "Application access is not provisioned")
            role_names = db.scalars(
                select(Role.name)
                .join(PrincipalRole, PrincipalRole.role_id == Role.id)
                .where(
                    PrincipalRole.principal_id == principal.subject,
                    Role.tenant_id == principal.tenant_id,
                )
            ).all()
        return Principal(principal.subject, principal.tenant_id, frozenset(role_names), principal.source)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, "Application authorization unavailable") from exc


def _sign_session(principal: Principal, now: int | None = None) -> str:
    issued = int(time.time() if now is None else now)
    payload = {
        "sub": principal.subject,
        "tenant_id": principal.tenant_id,
        "roles": sorted(principal.roles),
        "source": principal.source,
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
            source=str(payload.get("source", "idp")),
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


def principal_from_oidc_token(authorization: str | None = Header(default=None)) -> Principal:
    """Validate an OIDC access token and resolve its provisioned DB principal."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Bearer token required")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(401, "Invalid bearer token")
    try:
        metadata = discover()
        key = _signing_key(metadata, token)
        claims = jwt.decode(
            token,
            key=key,
            algorithms=["RS256", "ES256", "PS256"],
            audience=settings.identity_client_id,
            issuer=_issuer(),
            options={"require": ["exp", "iss", "sub", "aud"]},
        )
        identity = _principal_from_claims(claims)
        identity = Principal(identity.subject, identity.tenant_id, identity.roles, source="service")
        return _database_principal(identity, provision=False, service_only=True)
    except (jwt.PyJWKClientConnectionError, httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(503, "OIDC authorization unavailable") from exc
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(401, "Invalid bearer token") from exc


def principal_from_session(
    request: Request, authorization: str | None = Header(default=None)
) -> Principal:
    if authorization is not None:
        if settings.auth_mode.lower() == "oidc":
            return principal_from_oidc_token(authorization)
        if settings.auth_mode.lower() == "jwt":
            return principal_from_token(authorization)
        raise HTTPException(401, "Bearer authentication is disabled")
    if settings.auth_mode.lower() == "dev":
        # ponytail: local-only bypass; never enable AUTH_MODE=dev in IDC.
        return Principal("local-admin", "tenant-local", frozenset({"admin", "finance"}))
    session = request.cookies.get(settings.identity_session_cookie)
    if session:
        principal = _verify_session(session)
        # IdP identity is authentication only. Resolve business roles from the
        # application's own database so an IdP claim can never elevate access.
        return _database_principal(principal, provision=True)
    if settings.auth_mode.lower() == "jwt":
        return principal_from_token(authorization)
    raise HTTPException(401, "OIDC login required")


def _safe_oidc_endpoint(value: str, label: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"OIDC discovery returned an invalid {label}")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError(f"OIDC discovery returned an insecure {label}")
    return value


@lru_cache(maxsize=_DISCOVERY_CACHE_SIZE)
def _discover_cached(issuer: str, time_bucket: int) -> OIDCDiscovery:
    response = httpx.get(
        issuer + "/.well-known/openid-configuration",
        timeout=settings.identity_http_timeout_seconds,
        follow_redirects=False,
    )
    response.raise_for_status()
    data = response.json()
    try:
        discovered_issuer = _safe_oidc_endpoint(str(data["issuer"]), "issuer").rstrip("/")
        if discovered_issuer != issuer.rstrip("/"):
            raise RuntimeError("OIDC discovery issuer does not match configured issuer")
        authorization_endpoint = _safe_oidc_endpoint(
            str(data["authorization_endpoint"]), "authorization endpoint"
        )
        token_endpoint = _safe_oidc_endpoint(str(data["token_endpoint"]), "token endpoint")
        jwks_uri = _safe_oidc_endpoint(str(data["jwks_uri"]), "JWKS URI")
        userinfo_endpoint = data.get("userinfo_endpoint")
        end_session_endpoint = data.get("end_session_endpoint")
        return OIDCDiscovery(
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint,
            jwks_uri=jwks_uri,
            userinfo_endpoint=(_safe_oidc_endpoint(str(userinfo_endpoint), "userinfo endpoint")
                                if userinfo_endpoint else None),
            end_session_endpoint=(_safe_oidc_endpoint(str(end_session_endpoint), "logout endpoint")
                                  if end_session_endpoint else None),
            issuer=discovered_issuer,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("OIDC discovery response is incomplete") from exc


def discover() -> OIDCDiscovery:
    issuer = _issuer()
    return _discover_cached(issuer, int(time.monotonic() / _DISCOVERY_CACHE_TTL_SECONDS))


@lru_cache(maxsize=_JWKS_CACHE_SIZE)
def _jwks_client(uri: str, timeout: float) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(
        uri,
        timeout=timeout,
        cache_jwk_set=True,
        lifespan=_DISCOVERY_CACHE_TTL_SECONDS,
    )


def _cached_jwk_exists(client: jwt.PyJWKClient, kid: str) -> bool:
    cache = getattr(client, "jwk_set_cache", None)
    if cache is None or cache.get() is None:
        return False
    # PyJWT stores the cached response as a raw ``{"keys": [...]}`` dict,
    # while ``get_signing_keys`` converts it to PyJWKSet and applies its
    # signing-use filter.  Calling the public method here does not fetch while
    # the cache is live, and avoids treating encryption-only keys as usable.
    try:
        return any(key.key_id == kid for key in client.get_signing_keys())
    except jwt.PyJWKClientError:
        return False


def _signing_key(metadata: OIDCDiscovery, token: str):
    """Resolve a cached key while rate-limiting failed unknown-kid refreshes."""
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError:
        raise
    if not isinstance(kid, str) or not kid:
        raise jwt.PyJWKClientError("OIDC token has no signing key id")
    client = _jwks_client(metadata.jwks_uri, settings.identity_http_timeout_seconds)
    refresh_key = metadata.issuer or _issuer()
    if _cached_jwk_exists(client, kid):
        return client.get_signing_key_from_jwt(token).key
    with _JWKS_REFRESH_LOCK:
        if _cached_jwk_exists(client, kid):
            return client.get_signing_key_from_jwt(token).key
        now = time.monotonic()
        if _JWKS_REFRESH_UNTIL.get(refresh_key, 0.0) > now:
            raise jwt.PyJWKClientError("OIDC signing key refresh temporarily rate-limited")
        if len(_JWKS_REFRESH_UNTIL) >= _JWKS_REFRESH_MAX_ISSUERS:
            oldest = min(_JWKS_REFRESH_UNTIL, key=_JWKS_REFRESH_UNTIL.get)
            _JWKS_REFRESH_UNTIL.pop(oldest, None)
        _JWKS_REFRESH_UNTIL[refresh_key] = now + _JWKS_REFRESH_COOLDOWN_SECONDS
        key = client.get_signing_key_from_jwt(token).key
        _JWKS_REFRESH_UNTIL.pop(refresh_key, None)
        return key


def exchange_code(code: str, verifier: str, nonce: str) -> Principal:
    if not code or not verifier or not nonce:
        raise HTTPException(400, "Invalid OIDC callback")
    try:
        metadata = discover()
    except (RuntimeError, httpx.HTTPError) as exc:
        raise HTTPException(503, "OIDC discovery unavailable") from exc
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
        key = _signing_key(metadata, id_token)
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
    "browser_session_active",
    "discover",
    "exchange_code",
    "identity_account_url",
    "identity_logout_url",
    "new_pkce_pair",
    "principal_from_session",
    "principal_from_token",
    "principal_from_oidc_token",
    "safe_next_path",
    "_principal_from_claims",
    "_sign_session",
    "_verify_session",
]
