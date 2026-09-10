"""Opt-in account lifecycle on isolated PostgreSQL and Redis, without an IdP."""
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from redis import Redis
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app import db, local_admin, main
from app.config import settings


@pytest.mark.skipif(not (os.getenv("TEST_DATABASE_URL") and os.getenv("TEST_REDIS_URL")),
                    reason="requires disposable PostgreSQL and Redis")
def test_admin_created_local_user_password_and_access_lifecycle(monkeypatch):
    schema = "account_test_" + uuid4().hex
    control = create_engine(os.environ["TEST_DATABASE_URL"])
    with control.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = make_url(os.environ["TEST_DATABASE_URL"]).update_query_dict({"options": f"-csearch_path={schema}"})
    engine = create_engine(url)
    redis = Redis.from_url(os.environ["TEST_REDIS_URL"])
    session = sessionmaker(bind=engine, expire_on_commit=False)
    origin = "http://127.0.0.1:8000"
    client_address = (f"192.0.2.{int(schema[-2:], 16) % 254 + 1}", 50000)
    try:
        migration_env = dict(os.environ, DATABASE_URL=url.render_as_string(hide_password=False),
                             AUTH_MODE="jwt", OPENAI_API_KEY="test-placeholder",
                             JWT_SECRET="test-secret-at-least-32-characters")
        migration_code = (
            "from pydantic_settings.sources import DotEnvSettingsSource; "
            "DotEnvSettingsSource.__call__ = lambda self: {}; "
            "from alembic.config import Config; from alembic import command; "
            "command.upgrade(Config('alembic.ini'), 'head')"
        )
        subprocess.run([sys.executable, "-c", migration_code], env=migration_env,
                       cwd=Path(__file__).resolve().parents[1], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
        for module in (db, main, local_admin):
            monkeypatch.setattr(module, "SessionLocal", session)
        monkeypatch.setattr(main, "cache", redis)
        monkeypatch.setattr(settings, "auth_mode", "local")
        monkeypatch.setattr(settings, "identity_default_tenant_id", schema)
        monkeypatch.setattr(settings, "identity_session_secret", "synthetic-session-secret-at-least-32-characters")
        monkeypatch.setattr(settings, "identity_cookie_secure", False)
        monkeypatch.setattr(settings, "identity_redirect_uri", origin + "/auth/callback")
        monkeypatch.setattr(settings, "local_admin_username", "test-installer")
        monkeypatch.setattr(settings, "local_admin_password", "synthetic-installer-password")
        local_admin.ensure_local_admin()
        admin = TestClient(main.app, base_url=origin, client=client_address,
                           headers={"Origin": origin, "Referer": origin + "/login"})
        employee = TestClient(main.app, base_url=origin, client=client_address,
                              headers={"Origin": origin, "Referer": origin + "/login"})
        assert employee.get("/api/v1/documents").status_code == 401
        assert employee.get("/home", follow_redirects=False).status_code == 303
        assert employee.get("/admin/members", follow_redirects=False).status_code == 303
        assert employee.post("/api/v1/admin/members", json={"source": "idp", "subject": "anonymous"}).status_code == 401
        assert employee.post("/auth/register", json={}).status_code == 404
        assert admin.post("/auth/local/login", data={"username": "test-installer",
                          "password": "synthetic-installer-password"}, follow_redirects=False).status_code == 303
        rejected = admin.post("/api/v1/admin/members", json={"source": "local", "username": "invalid-user",
                               "password": "short-secret", "roles": []})
        assert rejected.status_code == 422
        assert "short-secret" not in rejected.text
        created = admin.post("/api/v1/admin/members", json={"source": "local", "username": "employee",
                              "password": "synthetic-employee-password", "roles": ["reader", "r" * 128]})
        assert created.status_code == 201
        subject = created.json()["subject"]
        assert employee.post("/auth/local/login", data={"username": "employee",
                             "password": "synthetic-employee-password"}, follow_redirects=False).status_code == 303
        assert employee.get("/api/v1/account").json()["source"] == "local"
        assert employee.get("/api/v1/documents").status_code == 200
        assert employee.get("/api/v1/questions/top").json() == {"items": []}
        assert admin.get("/api/v1/admin/questions").json() == {"items": []}
        assert employee.post("/api/v1/admin/members", json={"source": "idp", "subject": "forbidden"}).status_code == 403
        assert admin.post(f"/api/v1/admin/members/{subject}/password",
                          json={"new_password": "synthetic-replacement-password"}).status_code == 200
        assert employee.get("/api/v1/documents").status_code in {401, 403}
        assert employee.post("/auth/local/login", data={"username": "employee",
                             "password": "synthetic-employee-password"}, follow_redirects=False).status_code == 401
        assert employee.post("/auth/local/login", data={"username": "employee",
                             "password": "synthetic-replacement-password"}, follow_redirects=False).status_code == 303
        assert admin.put(f"/api/v1/admin/members/{subject}/status", json={"status": "disabled"}).status_code == 200
        assert admin.put(f"/api/v1/admin/members/{subject}/status", json={"status": "active"}).status_code == 200
        assert employee.get("/api/v1/documents").status_code in {401, 403}
        with session() as store:
            credential = store.get(db.LocalUserCredential, subject)
            assert credential.password_hash != "synthetic-replacement-password"
            assert store.query(db.Principal).count() == 2
            assert store.query(db.AuditEvent).count() >= 4
    finally:
        redis.close()
        engine.dispose()
        with control.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        control.dispose()
