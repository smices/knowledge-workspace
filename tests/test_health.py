from app import main


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement):
        assert "SELECT 1" in str(statement)


def test_health_reports_dependency_checks(monkeypatch):
    monkeypatch.setattr(main.engine, "connect", lambda: _Connection())
    monkeypatch.setattr(main.cache, "ping", lambda: True)
    monkeypatch.setattr(main.vector_client, "get_collections", lambda: object())
    assert main.health() == {
        "status": "ok",
        "checks": {"database": "ok", "redis": "ok", "qdrant": "ok"},
    }
