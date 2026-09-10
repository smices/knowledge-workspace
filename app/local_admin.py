"""Installation-created local administrator authentication."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.auth import Principal
from app.config import settings
from app.db import (LocalAdminCredential, LocalUserCredential, Principal as DbPrincipal,
                    PrincipalRole, Role, SessionLocal, Tenant)

LOCAL_ADMIN_ID = "local:initial-admin"
LOCAL_ADMIN_CREDENTIAL_ID = "initial"
# Legacy installer hashes predate the 128-character creation bound.  This
# applies only when verifying the existing initial administrator credential.
LOCAL_ADMIN_PASSWORD_MAX_LENGTH = 4096
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode("ascii"))


def normalize_username(username: str) -> str:
    if not isinstance(username, str):
        raise ValueError("username must be text")
    normalized = username.strip().casefold()
    try:
        normalized.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("username must contain ASCII characters only") from exc
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError("username contains unsupported characters")
    return normalized


def validate_password(password: str) -> str:
    if not isinstance(password, str) or not 16 <= len(password) <= 128:
        raise ValueError("password must be between 16 and 128 characters")
    return password


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
    except (ValueError, TypeError, OverflowError):
        return False


_DUMMY_PASSWORD_HASH: str | None = None


def _dummy_password_hash() -> str:
    global _DUMMY_PASSWORD_HASH
    if _DUMMY_PASSWORD_HASH is None:
        _DUMMY_PASSWORD_HASH = hash_password("dummy-local-login-password")
    return _DUMMY_PASSWORD_HASH


def ensure_local_admin() -> None:
    """Create the configured account once; never reset it from configuration."""
    with SessionLocal() as db:
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(734106873)"))
        existing = db.get(LocalAdminCredential, LOCAL_ADMIN_CREDENTIAL_ID)
        if existing:
            account = db.get(DbPrincipal, existing.principal_id)
            if (account is None or account.principal_type != "local_admin"
                    or account.tenant_id != (settings.identity_default_tenant_id or "tenant-local")):
                raise RuntimeError("local administrator credential has no principal")
            # Pre-Phase1 installations accepted looser installer usernames.  Keep
            # those credentials usable during upgrade; normalize legacy values
            # that are now safe, but do not silently delete an existing account
            # whose username cannot satisfy the new creation policy.
            try:
                normalized_existing = normalize_username(existing.username)
            except ValueError:
                normalized_existing = None
            if normalized_existing and existing.username != normalized_existing:
                existing.username = normalized_existing
                db.commit()
            return
        try:
            username = normalize_username(settings.local_admin_username or "")
            password = validate_password(settings.local_admin_password or "")
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if not username or not password:
            raise RuntimeError("LOCAL_ADMIN_USERNAME and LOCAL_ADMIN_PASSWORD are required for the first installation")
        if "replace_with" in password.lower() or "replace-with" in password.lower():
            raise RuntimeError("LOCAL_ADMIN_PASSWORD must replace the deployment placeholder")
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
            role = Role(id=str(uuid4()), tenant_id=tenant_id, name="admin")
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
        try:
            normalized = normalize_username(username)
        except ValueError:
            normalized = ""
        legacy_username = username.strip() if isinstance(username, str) else ""
        row = db.execute(select(LocalAdminCredential, DbPrincipal).join(
            DbPrincipal, DbPrincipal.id == LocalAdminCredential.principal_id
        ).where(LocalAdminCredential.username == normalized)).first() if normalized else None
        if row is None and legacy_username and legacy_username != normalized:
            row = db.execute(select(LocalAdminCredential, DbPrincipal).join(
                DbPrincipal, DbPrincipal.id == LocalAdminCredential.principal_id
            ).where(LocalAdminCredential.username == legacy_username)).first()
        credential = row[0] if row else None
        account = row[1] if row else None
        password_valid = (isinstance(password, str)
                          and 16 <= len(password) <= LOCAL_ADMIN_PASSWORD_MAX_LENGTH)
        candidate = (password if isinstance(password, str)
                     and len(password) <= LOCAL_ADMIN_PASSWORD_MAX_LENGTH
                     else "x" * LOCAL_ADMIN_PASSWORD_MAX_LENGTH)
        password_hash = credential.password_hash if credential is not None else _dummy_password_hash()
        if not password_valid or not verify_password(candidate, password_hash):
            return None
        if account is None or account.status != "active" or account.principal_type != "local_admin":
            return None
        roles = db.scalars(select(Role.name).join(PrincipalRole, PrincipalRole.role_id == Role.id).where(
            PrincipalRole.principal_id == account.id, Role.tenant_id == account.tenant_id
        )).all()
        return Principal(account.id, account.tenant_id, frozenset(roles), source="local",
                         session_version=account.session_version)


def authenticate_local_user(username: str, password: str, tenant_id: str | None = None) -> Principal | None:
    try:
        normalized = normalize_username(username)
    except ValueError:
        normalized = ""
    with SessionLocal() as db:
        query = select(LocalUserCredential, DbPrincipal).join(
            DbPrincipal, DbPrincipal.id == LocalUserCredential.principal_id
        ).where(LocalUserCredential.username == normalized)
        if tenant_id:
            query = query.where(LocalUserCredential.tenant_id == tenant_id)
        row = db.execute(query).first() if normalized else None
        credential = row[0] if row else None
        account = row[1] if row else None
        password_hash = credential.password_hash if credential is not None else _dummy_password_hash()
        password_valid = isinstance(password, str) and 16 <= len(password) <= 128
        candidate = password if isinstance(password, str) and len(password) <= 128 else "x" * 128
        valid = verify_password(candidate, password_hash)
        if not password_valid or not valid or credential is None:
            return None
        if (account is None or account.status != "active" or account.principal_type != "local_user"
                or account.tenant_id != credential.tenant_id):
            return None
        roles = db.scalars(select(Role.name).join(PrincipalRole, PrincipalRole.role_id == Role.id).where(
            PrincipalRole.principal_id == account.id, Role.tenant_id == account.tenant_id
        )).all()
        return Principal(account.id, account.tenant_id, frozenset(roles), source="local",
                         session_version=account.session_version)


def authenticate_local(username: str, password: str, tenant_id: str | None = None) -> Principal | None:
    """Authenticate either the installation admin or a tenant local user."""
    try:
        normalized = normalize_username(username)
    except ValueError:
        normalized = ""
    with SessionLocal() as db:
        row = None
        if normalized:
            row = db.execute(select(LocalAdminCredential, DbPrincipal).join(
                DbPrincipal, DbPrincipal.id == LocalAdminCredential.principal_id
            ).where(LocalAdminCredential.username == normalized)).first()
        legacy_username = username.strip() if isinstance(username, str) else ""
        if row is None and legacy_username and legacy_username != normalized:
            row = db.execute(select(LocalAdminCredential, DbPrincipal).join(
                DbPrincipal, DbPrincipal.id == LocalAdminCredential.principal_id
            ).where(LocalAdminCredential.username == legacy_username)).first()
        admin = row[0] if row else None
        user = None
        if row is None and normalized and tenant_id:
            row = db.execute(select(LocalUserCredential, DbPrincipal).join(
                DbPrincipal, DbPrincipal.id == LocalUserCredential.principal_id
            ).where(LocalUserCredential.username == normalized,
                    LocalUserCredential.tenant_id == tenant_id)).first()
            user = row[0] if row else None
        credential = row[0] if row else None
        account = row[1] if row else None
        credential_hash = credential.password_hash if credential is not None else _dummy_password_hash()
        max_password_length = (LOCAL_ADMIN_PASSWORD_MAX_LENGTH
                               if account is not None and account.principal_type == "local_admin"
                               else 128)
        password_valid = isinstance(password, str) and 16 <= len(password) <= max_password_length
        candidate = (password if isinstance(password, str) and len(password) <= max_password_length
                     else "x" * max_password_length)
        if not password_valid or not verify_password(candidate, credential_hash):
            return None
        if (account is None or credential is None or account.status != "active"
                or account.principal_type not in {"local_admin", "local_user"}
                or (admin is not None and account.principal_type != "local_admin")
                or (user is not None and (account.principal_type != "local_user"
                                          or account.tenant_id != user.tenant_id))):
            return None
        roles = db.scalars(select(Role.name).join(PrincipalRole, PrincipalRole.role_id == Role.id).where(
            PrincipalRole.principal_id == account.id, Role.tenant_id == account.tenant_id
        )).all()
        return Principal(account.id, account.tenant_id, frozenset(roles), source="local",
                         session_version=account.session_version)
