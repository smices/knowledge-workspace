from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient
import pytest

from app import db, local_admin, main
from app.auth import Principal, _sign_session
from app.config import Settings, settings


class FakeLoginLimiter:
    def __init__(self):
        self.counts = {}
        self.keys = []
        self.fail = False

    def eval(self, _script, count, ip_key, user_key, _ttl):
        if self.fail:
            raise RuntimeError("redis unavailable")
        assert count == 2
        self.keys.append((ip_key, user_key))
        self.counts[ip_key] = self.counts.get(ip_key, 0) + 1
        self.counts[user_key] = self.counts.get(user_key, 0) + 1
        return [self.counts[ip_key], self.counts[user_key]]


def _setup(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", session)
    monkeypatch.setattr(local_admin, "SessionLocal", session)
    monkeypatch.setattr(main, "SessionLocal", session)
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "identity_default_tenant_id", "tenant-1")
    monkeypatch.setattr(settings, "identity_session_secret", "s" * 40)
    monkeypatch.setattr(settings, "local_admin_username", "installer")
    monkeypatch.setattr(settings, "local_admin_password", "installer-password-16")
    limiter = FakeLoginLimiter()
    monkeypatch.setattr(main, "cache", limiter)
    local_admin.ensure_local_admin()
    return session, limiter


def _origin_headers():
    return {"Origin": "http://testserver"}


def _admin_cookie():
    return _sign_session(Principal(local_admin.LOCAL_ADMIN_ID, "tenant-1", frozenset({"admin"}), source="local"))


def test_auth_options_and_local_mode_reject_bearer(monkeypatch):
    _setup(monkeypatch)
    client = TestClient(main.app)
    assert client.get("/auth/options").json() == {
        "local_login": True, "idp_login": False, "registration_open": False,
    }
    response = client.get("/api/v1/account", headers={"Authorization": "Bearer anything"})
    assert response.status_code == 401
    assert client.get("/auth/callback?code=unused&state=unused").status_code == 404


def test_jwt_mode_does_not_accept_a_browser_session_cookie(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(settings, "auth_mode", "jwt")
    client = TestClient(main.app)
    client.cookies.set(settings.identity_session_cookie, _admin_cookie())
    assert client.get("/api/v1/account").status_code == 403


def test_local_mode_rejects_short_explicit_session_secret():
    with pytest.raises(ValueError, match="session secret"):
        Settings(database_url="sqlite://", openai_api_key="test", jwt_secret="j" * 32,
                 auth_mode="local", identity_session_secret="short", _env_file=None)


@pytest.mark.parametrize("field", ["jwt_secret", "identity_session_secret"])
def test_non_dev_rejects_secret_placeholders(field):
    values = {
        "database_url": "sqlite://", "openai_api_key": "test", "jwt_secret": "j" * 32,
        "auth_mode": "local", "identity_session_secret": "i" * 40, "_env_file": None,
    }
    values[field] = "replace-with-a-random-secret-of-at-least-32-characters"
    with pytest.raises(ValueError, match="placeholder"):
        Settings(**values)


def test_long_role_names_use_bounded_ids_and_keep_name_lookup(monkeypatch):
    _setup(monkeypatch)
    principal = Principal(local_admin.LOCAL_ADMIN_ID, "tenant-1", frozenset({"admin"}), source="local")
    role = main.create_role(main.RoleRequest(name="r" * 128), principal)
    assert len(role["id"]) == 36
    assert role["name"] == "r" * 128


def test_local_login_origin_referer_and_independent_rate_keys(monkeypatch):
    _, limiter = _setup(monkeypatch)
    client = TestClient(main.app)
    denied = client.post("/auth/local/login", data={"username": "installer", "password": "installer-password-16"})
    assert denied.status_code == 403
    accepted = client.post("/auth/local/login", data={"username": "installer", "password": "installer-password-16"},
                           headers={"Referer": "http://testserver/login"}, follow_redirects=False)
    assert accepted.status_code == 303
    assert len(limiter.keys) == 1
    assert limiter.keys[0][0] != limiter.keys[0][1]


def test_member_sources_account_and_password_reset_invalidate_cookie(monkeypatch):
    session, _ = _setup(monkeypatch)
    client = TestClient(main.app)
    client.cookies.set(settings.identity_session_cookie, _admin_cookie())
    local = client.post("/api/v1/admin/members", headers=_origin_headers(), json={
        "source": "local", "username": "Alice", "password": "alice-password-16", "roles": ["reader"],
    })
    assert local.status_code == 201
    local_subject = local.json()["subject"]
    idp = client.post("/api/v1/admin/members", headers=_origin_headers(), json={
        "source": "idp", "subject": "idp-user", "roles": ["reader"],
    })
    assert idp.status_code == 201
    assert idp.json()["source"] == "idp"
    mixed = client.post("/api/v1/admin/members", headers=_origin_headers(), json={
        "source": "local", "username": "bad", "password": "bad-password-16", "subject": "idp-user",
    })
    assert mixed.status_code == 422
    assert "bad-password-16" not in mixed.text

    login = client.post("/auth/local/login", headers=_origin_headers(), data={
        "username": " ALICE ", "password": "alice-password-16",
    }, follow_redirects=False)
    assert login.status_code == 303
    assert client.get("/api/v1/account").json()["username"] == "alice"
    old_cookie = login.cookies.get(settings.identity_session_cookie)
    reset = client.post("/api/v1/account/password", headers=_origin_headers(), json={
        "current_password": "alice-password-16", "new_password": "alice-password-17",
    })
    assert reset.status_code == 200
    client.cookies.set(settings.identity_session_cookie, old_cookie)
    assert client.get("/api/v1/account").status_code == 401

    client.cookies.set(settings.identity_session_cookie, _admin_cookie())
    admin_reset = client.post(f"/api/v1/admin/members/{local_subject}/password", headers=_origin_headers(), json={
        "new_password": "alice-password-18",
    })
    assert admin_reset.status_code == 200
    with session() as store:
        assert store.get(db.Principal, local_subject).session_version == 2


def test_idp_cookie_is_preprovisioned_and_admin_pages_are_gated(monkeypatch):
    session, _ = _setup(monkeypatch)
    with session() as store:
        store.add(db.Principal(id="idp-user", tenant_id="tenant-1", principal_type="idp"))
        store.commit()
    client = TestClient(main.app)
    unknown_cookie = _sign_session(Principal("unknown-idp", "tenant-1", frozenset()))
    client.cookies.set(settings.identity_session_cookie, unknown_cookie)
    assert client.get("/api/v1/account").status_code == 403
    with session() as store:
        assert store.get(db.Principal, "unknown-idp") is None
    client.cookies.set(settings.identity_session_cookie,
                       _sign_session(Principal("idp-user", "tenant-1", frozenset())))
    assert client.get("/admin/", follow_redirects=False).status_code == 403

    anonymous = TestClient(main.app)
    assert anonymous.get("/admin/", follow_redirects=False).headers["location"] == "/login"


def test_status_disable_reenable_invalidates_old_cookie_and_redis_failure_fails_closed(monkeypatch):
    session, limiter = _setup(monkeypatch)
    client = TestClient(main.app)
    client.cookies.set(settings.identity_session_cookie, _admin_cookie())
    created = client.post("/api/v1/admin/members", headers=_origin_headers(), json={
        "source": "local", "username": "bob", "password": "bob-password-016", "roles": [],
    })
    assert created.status_code == 201, created.text
    subject = created.json()["subject"]
    user = TestClient(main.app)
    login = user.post("/auth/local/login", headers=_origin_headers(), data={
        "username": "bob", "password": "bob-password-016",
    })
    old_cookie = login.cookies.get(settings.identity_session_cookie)
    client.put(f"/api/v1/admin/members/{subject}/status", headers=_origin_headers(), json={"status": "disabled"})
    client.put(f"/api/v1/admin/members/{subject}/status", headers=_origin_headers(), json={"status": "active"})
    user.cookies.set(settings.identity_session_cookie, old_cookie)
    assert user.get("/api/v1/account").status_code == 401

    limiter.fail = True
    response = client.post("/auth/local/login", headers=_origin_headers(), data={
        "username": "bob", "password": "bob-password-016",
    })
    assert response.status_code == 503


def test_legacy_installer_username_survives_upgrade_and_overlong_password_fails(monkeypatch):
    session, _ = _setup(monkeypatch)
    old_password = "x" * 4096
    with session() as store:
        credential = store.get(db.LocalAdminCredential, local_admin.LOCAL_ADMIN_CREDENTIAL_ID)
        credential.username = "安装管理员"
        credential.password_hash = local_admin.hash_password(old_password)
        store.commit()

    # Existing installations may have used non-ASCII installer names before
    # the stricter creation policy.  ensure_local_admin must preserve them.
    local_admin.ensure_local_admin()

    client = TestClient(main.app)
    login = client.post("/auth/local/login", headers=_origin_headers(), data={
        "username": " 安装管理员 ", "password": old_password,
    }, follow_redirects=False)
    assert login.status_code == 303
    old_cookie = login.cookies.get(settings.identity_session_cookie)
    assert old_cookie
    changed = client.post("/api/v1/account/password", headers=_origin_headers(), json={
        "current_password": old_password, "new_password": "new-admin-password-16",
    })
    assert changed.status_code == 200
    client.cookies.set(settings.identity_session_cookie, old_cookie)
    assert client.get("/api/v1/account").status_code == 401
    assert local_admin.authenticate_local("安装管理员", old_password) is None
    assert local_admin.authenticate_local("安装管理员", "x" * 4097) is None
    assert local_admin.authenticate_local("安装管理员", "new-admin-password-16")
