from types import SimpleNamespace

from app.main import alias_bindings
from app.vector import _rank_points
from worker.main import alias_candidates


def test_approved_aliases_preserve_same_evidence_entity_gate():
    points = [
        SimpleNamespace(payload={"content": "寶玉獨自賞花"}, score=0.9),
        SimpleNamespace(payload={"content": "寶玉與黛玉相對而泣"}, score=0.8),
    ]
    aliases = {"賈寶玉": ["寶玉"], "林黛玉": ["黛玉"]}
    result = _rank_points(points, "贾宝玉和林黛玉是什么关系？", 5, aliases)
    assert [item.payload["content"] for item in result] == ["寶玉與黛玉相對而泣"]


def test_alias_binding_and_pattern_candidates_are_evidence_bound():
    records = [{"id": "a1", "canonical": "賈寶玉", "alias": "寶玉", "document_id": "d1",
                "document_version": 1, "confidence": 0.92}]
    assert alias_bindings([{"document_id": "d1", "document_version": 1, "content": "寶玉與黛玉"}], records) == [{
        "entity": "賈寶玉", "matched_mention": "寶玉", "alias_id": "a1",
        "status": "approved", "confidence": 0.92, "evidence": [1],
    }]
    candidates = list(alias_candidates([("PaymentService（简称 PaySvc）", [], None)]))
    assert candidates[0][1:3] == ("PaymentService", "PaySvc")


def test_scoped_alias_cannot_bind_or_pass_another_document():
    aliases = {"賈寶玉": [{"alias": "寶玉", "document_id": "d1", "document_version": 1}],
               "林黛玉": [{"alias": "黛玉", "document_id": "d1", "document_version": 1}]}
    points = [SimpleNamespace(payload={"document_id": "d2", "document_version": 1, "content": "寶玉與黛玉"}, score=0.9)]
    assert _rank_points(points, "贾宝玉和林黛玉是什么关系？", 5, aliases) == []
