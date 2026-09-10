import json
import logging
from uuid import UUID

from fastapi.testclient import TestClient
from app.main import app
from app.vector import VersionScopeOverflow


def test_request_metrics_exclude_query_values_and_arbitrary_paths(caplog):
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        response = TestClient(app).get("/live?token=synthetic-sensitive-query")
        missing = TestClient(app).get("/unknown-sensitive-path")
    assert response.status_code == 200
    UUID(response.headers["X-Request-ID"])
    assert missing.status_code == 404
    records = [json.loads(row.message) for row in caplog.records if row.name == "uvicorn.error"]
    assert [row["status"] for row in records] == [200, 404]
    assert records[0]["route"] == "/live"
    assert "synthetic-sensitive-query" not in caplog.text
    assert "unknown-sensitive-path" not in caplog.text


def test_version_scope_overflow_is_a_controlled_unavailable_response(monkeypatch):
    route = next(route for route in app.routes if getattr(route, "path", None) == "/live")
    def overflow():
        raise VersionScopeOverflow("synthetic internal scope")
    monkeypatch.setattr(route.dependant, "call", overflow)
    response = TestClient(app).get("/live")
    assert response.status_code == 503
    assert response.json() == {"detail": "Document scope exceeds retrieval capacity"}
