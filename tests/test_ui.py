from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import db
from app.auth import Principal, _sign_session
from app.config import settings
from app.main import app


def test_root_routes_anonymous_browsers_to_login_and_removes_ui(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "oidc")
    client = TestClient(app)

    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/home", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/ui", follow_redirects=False).status_code == 404
    assert client.get("/ui/", follow_redirects=False).status_code == 404


def test_authenticated_browser_reaches_home_with_one_status_label(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "oidc")
    monkeypatch.setattr(settings, "identity_session_secret", "s" * 40)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", session)
    with session() as store:
        store.add(db.Tenant(id="tenant-1", name="Tenant 1"))
        store.add(db.Principal(id="user-1", tenant_id="tenant-1", principal_type="idp"))
        store.commit()
    client = TestClient(app)
    client.cookies.set(settings.identity_session_cookie, _sign_session(Principal("user-1", "tenant-1", frozenset())))

    root = client.get("/", follow_redirects=False)
    home = client.get("/home")

    assert root.headers["location"] == "/home"
    assert home.status_code == 200
    assert home.text.count("已连接知识库") == 1
    assert 'href="/admin/"' not in home.text
    assert '/assets/styles.css' in home.text


def test_dev_login_creates_a_local_session_and_enters_home(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "dev")
    client = TestClient(app)

    response = client.get("/auth/login", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/home"
    assert settings.identity_session_cookie in response.headers["set-cookie"]
