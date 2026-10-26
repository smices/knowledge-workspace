import threading

import pytest

from app import global_ids


class _TestAuthority:
    def __init__(self):
        self.lock = threading.Lock()
        self.value = 0

    def next(self, count=1):
        with self.lock:
            values = [str(self.value + index + 1) for index in range(count)]
            self.value += count
        return values


@pytest.fixture(autouse=True)
def mock_global_id_authority(monkeypatch, request):
    if request.module.__name__.endswith("test_pipeline_integration"):
        return
    monkeypatch.setattr(global_ids, "_client", _TestAuthority())
