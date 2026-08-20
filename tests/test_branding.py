import json

import pytest
from pydantic import ValidationError

from app import main
from app.config import Settings


def test_brand_script_uses_runtime_settings(monkeypatch):
    monkeypatch.setattr(main.settings, "brand_name", "Atlas Library")
    monkeypatch.setattr(main.settings, "brand_mark", "AL")
    monkeypatch.setattr(main.settings, "brand_primary_color", "#123456")

    response = main.brand_script()
    body = response.body.decode()
    payload = json.loads(body.removeprefix("window.__APP_BRAND__=").removesuffix(";"))

    assert payload["name"] == "Atlas Library"
    assert payload["mark"] == "AL"
    assert payload["primaryColor"] == "#123456"
    assert response.headers["cache-control"] == "no-store"


def test_brand_name_cannot_be_blank():
    with pytest.raises(ValidationError):
        Settings(database_url="sqlite://", openai_api_key="test", jwt_secret="test",
                 auth_mode="dev", brand_name="   ", _env_file=None)


def test_non_development_auth_requires_secure_jwt_secret():
    with pytest.raises(ValidationError):
        Settings(database_url="sqlite://", openai_api_key="test", jwt_secret="short",
                 auth_mode="jwt", _env_file=None)
