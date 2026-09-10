"""Installation-created local administrator authentication."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.auth import Principal
from app.config import settings
from app.db import LocalAdminCredential, Principal as DbPrincipal, PrincipalRole, Role, SessionLocal, Tenant

LOCAL_ADMIN_ID = "local:initial-admin"
LOCAL_ADMIN_CREDENTIAL_ID = "initial"


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode("ascii"))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt$16384$8$1${_encode(salt)}${_encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, digest = encoded.split("$")
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=_decode(salt), n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(actual, _decode(digest))
    except (ValueError, TypeError):
        return False


def ensure_local_admin() -> None:
    """Create the configured account once; never reset it from configuration."""
    with SessionLocal() as db:
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(734106873)"))
        existing = db.get(LocalAdminCredential, LOCAL_ADMIN_CREDENTIAL_ID)
        if existing:
            if db.get(DbPrincipal, existing.principal_id) is None:
                raise RuntimeError("local administrator credential has no principal")
            return
        username = (settings.local_admin_username or "").strip()
        password = settings.local_admin_password or ""
        if not username or not password:
            raise RuntimeError("LOCAL_ADMIN_USERNAME and LOCAL_ADMIN_PASSWORD are required for the first installation")
        if len(username) > 64 or len(password) < 16:
            raise RuntimeError("LOCAL_ADMIN_USERNAME must be at most 64 characters and LOCAL_ADMIN_PASSWORD at least 16")
        tenant_id = settings.identity_default_tenant_id or "tenant-local"
        if db.get(Tenant, tenant_id) is None:
            db.add(Tenant(id=tenant_id, name=tenant_id))
        account = db.get(DbPrincipal, LOCAL_ADMIN_ID)
        if account is None:
            account = DbPrincipal(id=LOCAL_ADMIN_ID, tenant_id=tenant_id, display_name="Initial local administrator",
                                  principal_type="local_admin")
            db.add(account)
        role = db.scalar(select(Role).where(Role.tenant_id == tenant_id, Role.name == "admin"))
        if role is None:
            role = Role(id=f"{tenant_id}:admin", tenant_id=tenant_id, name="admin")
            db.add(role)
            db.flush()
        if db.get(PrincipalRole, {"principal_id": LOCAL_ADMIN_ID, "role_id": role.id}) is None:
            db.add(PrincipalRole(principal_id=LOCAL_ADMIN_ID, role_id=role.id))
        db.add(LocalAdminCredential(id=LOCAL_ADMIN_CREDENTIAL_ID, principal_id=LOCAL_ADMIN_ID,
                                    username=username, password_hash=hash_password(password)))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if db.get(LocalAdminCredential, LOCAL_ADMIN_CREDENTIAL_ID) is None:
                raise


def authenticate_local_admin(username: str, password: str) -> Principal | None:
    with SessionLocal() as db:
        credential = db.scalar(select(LocalAdminCredential).where(LocalAdminCredential.username == username.strip()))
        if credential is None or not verify_password(password, credential.password_hash):
            return None
        account = db.get(DbPrincipal, credential.principal_id)
        if account is None or account.status != "active" or account.principal_type != "local_admin":
            return None
        roles = db.scalars(select(Role.name).join(PrincipalRole, PrincipalRole.role_id == Role.id).where(
            PrincipalRole.principal_id == account.id, Role.tenant_id == account.tenant_id
        )).all()
        return Principal(account.id, account.tenant_id, frozenset(roles), source="local")
