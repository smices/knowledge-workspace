"""Optional PostgreSQL auth concurrency regressions.

These tests are skipped in the normal SQLite unit-test run.  The production
row-lock behavior is only meaningful against PostgreSQL, so run them with an
isolated TEST_DATABASE_URL when validating the deployment database.
"""

from concurrent.futures import ThreadPoolExecutor
import os
import time
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app import auth, db, main
from app.auth import Principal
from app.config import settings
from app.local_admin import hash_password, verify_password


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="requires isolated TEST_DATABASE_URL")
def test_postgres_password_resets_serialize_session_version(monkeypatch):
    url = make_url(os.environ["TEST_DATABASE_URL"])
    if url.get_backend_name() != "postgresql":
        pytest.skip("requires PostgreSQL row locks")
    schema = "auth_concurrency_" + uuid4().hex
    control = create_engine(url)
    with control.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    application_name = "auth_concurrency_" + uuid4().hex[:12]
    engine = create_engine(url.update_query_dict({"options": f"-csearch_path={schema}"}),
                           connect_args={"application_name": application_name},
                           pool_size=4, max_overflow=0)
    session = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        db.Base.metadata.create_all(engine)
        with session() as store:
            store.add(db.Tenant(id="tenant-auth", name="Auth test"))
            store.flush()
            store.add(db.Principal(id="admin-auth", tenant_id="tenant-auth",
                                   principal_type="local_admin"))
            store.add(db.Principal(id="local:user:auth", tenant_id="tenant-auth",
                                   principal_type="local_user", display_name="auth"))
            store.add(db.Principal(id="local:user:stale", tenant_id="tenant-auth",
                                   principal_type="local_user", display_name="stale"))
            store.flush()
            store.add(db.LocalUserCredential(
                principal_id="local:user:auth", tenant_id="tenant-auth", username="auth",
                password_hash=hash_password("initial-auth-password"),
            ))
            store.add(db.LocalUserCredential(
                principal_id="local:user:stale", tenant_id="tenant-auth", username="stale",
                password_hash=hash_password("initial-stale-password"),
            ))
            store.commit()

        monkeypatch.setattr(db, "SessionLocal", session)
        monkeypatch.setattr(main, "SessionLocal", session)
        monkeypatch.setattr(settings, "identity_redirect_uri", "http://testserver/auth/callback")
        actor = Principal("admin-auth", "tenant-auth", frozenset({"admin"}), source="local")

        def reset(subject: str, password: str):
            from starlette.requests import Request

            request = Request({
                "type": "http", "method": "POST", "path": f"/api/v1/admin/members/{subject}/password",
                "scheme": "http", "server": ("testserver", 80),
                "headers": [(b"origin", b"http://testserver")],
            })
            return main.reset_member_password(
                request, subject, main.PasswordResetRequest(new_password=password), actor
            )

        # A stale actor must fail after an administrator reset, even if the
        # caller passed dependency authentication before the reset committed.
        stale_actor = Principal("local:user:stale", "tenant-auth", frozenset(), source="local")
        reset("local:user:stale", "reset-stale-password")
        change_request = Request({
            "type": "http", "method": "POST", "path": "/api/v1/account/password",
            "scheme": "http", "server": ("testserver", 80),
            "headers": [(b"origin", b"http://testserver")],
        })
        with session() as store:
            before = store.get(db.LocalUserCredential, "local:user:stale").password_hash
        with pytest.raises(HTTPException) as stale_error:
            main.change_account_password(
                change_request,
                main.AccountPasswordRequest(current_password="reset-stale-password",
                                             new_password="stale-change-password"),
                Response(), stale_actor,
            )
        assert stale_error.value.status_code == 401
        with session() as store:
            credential = store.get(db.LocalUserCredential, "local:user:stale")
            assert store.get(db.Principal, "local:user:stale").session_version == 1
            assert credential.password_hash == before
            assert verify_password("reset-stale-password", credential.password_hash)

        # Hold the row lock so both workers are observed waiting before the
        # release.  This makes the regression deterministic rather than merely
        # relying on thread scheduling.
        locker = engine.connect()
        locker.execute(text("BEGIN"))
        locker.execute(text("SELECT id FROM principals WHERE id = :subject FOR UPDATE"),
                       {"subject": "local:user:auth"})
        executor = ThreadPoolExecutor(max_workers=2)
        futures = [executor.submit(reset, "local:user:auth", password)
                   for password in ("reset-auth-password-1", "reset-auth-password-2")]
        try:
            deadline = time.monotonic() + 5
            waiting = 0
            while time.monotonic() < deadline:
                with control.connect() as probe:
                    waiting = probe.scalar(text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND application_name = :application_name "
                        "AND wait_event_type = 'Lock' AND state = 'active'"
                    ), {"application_name": application_name}) or 0
                if waiting >= 2:
                    break
                time.sleep(0.05)
            assert waiting >= 2
        finally:
            locker.execute(text("ROLLBACK"))
            locker.close()
            executor.shutdown(wait=True)
        results = [future.result(timeout=10) for future in futures]
        assert all(item["reset"] for item in results)
        with session() as store:
            assert store.get(db.Principal, "local:user:auth").session_version == 2

        with pytest.raises(HTTPException) as expired:
            auth._database_principal(
                Principal("local:user:auth", "tenant-auth", frozenset(), source="local"),
                provision=False, check_session=True,
            )
        assert expired.value.status_code == 401
    finally:
        engine.dispose()
        with control.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        control.dispose()
