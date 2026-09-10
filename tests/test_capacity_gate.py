import math
import time

import pytest

from eval.gate import evaluate
from eval.load import LoadConfig, Sample, _NoRedirect, aggregate, request_one, request_spec


def passing_result():
    retrieval_item = {
        "id": "q1", "expected_titles": ["doc"], "hit": True,
        "mrr": 1.0, "ndcg": 1.0, "status": 200, "ms": 100,
    }
    answer_item = {
        "id": "q1", "expected_keywords": ["term"], "keywords": ["term"],
        "expected_answer_state": None, "answer_state": "answered", "quality": None,
        "hit": True, "citation_count": 1, "contract_support_rate": 1.0,
        "evidence_hash": "hash-1", "status": 200, "ms": 100,
    }
    return {
        "run_id": "run-1", "question_count": 1,
        "retrieval": {
            "success_rate": 1.0, "p95_ms": 100, "hit_rate": 1.0,
            "mrr": 1.0, "ndcg": 1.0, "items": [retrieval_item],
        },
        "answer": {
            "success_rate": 1.0, "p95_ms": 100, "hit_rate": 1.0,
            "keyword_rate": 1.0, "citation_rate": 1.0,
            "contract_support_rate": 1.0, "items": [answer_item],
        },
    }


def reviewed(run_id="run-1", evidence_hash=None):
    item = {"id": "q1", "status": "reviewed", "run_id": run_id}
    if evidence_hash:
        item["evidence_hash"] = evidence_hash
    return [item]


def test_gate_enforces_latency_ndcg_and_pending_reviews():
    result = passing_result()
    result["retrieval"]["p95_ms"] = 2_001
    result["retrieval"]["ndcg"] = 0.74
    result["answer"]["p95_ms"] = 60_001
    outcome = evaluate(result, [{"id": "q1", "status": "pending", "run_id": "run-1"}])
    assert outcome["ok"] is False
    assert "retrieval p95 > 2 seconds" in outcome["failures"]
    assert "retrieval NDCG < 0.75" in outcome["failures"]
    assert "answer p95 > 60 seconds" in outcome["failures"]
    assert "1 manual reviews pending" in outcome["failures"]


def test_gate_rejects_explicit_error_rate_even_when_success_rate_is_stale():
    result = passing_result()
    result["retrieval"]["error_rate"] = 0.06
    outcome = evaluate(result, reviewed())
    assert "retrieval error rate > 5%" in outcome["failures"]
    assert "retrieval.error_rate does not match samples" in outcome["failures"]


def test_success_rate_boundary_does_not_fail_from_float_rounding():
    result = passing_result()
    retrieval_items = []
    answer_items = []
    reviews = []
    for index in range(20):
        item_id = f"q{index + 1}"
        retrieval_items.append({
            "id": item_id, "expected_titles": ["doc"], "hit": True,
            "mrr": 1.0, "ndcg": 1.0, "status": 200 if index else 500, "ms": 100,
        })
        answer_items.append({
            "id": item_id, "expected_keywords": ["term"], "keywords": ["term"],
            "expected_answer_state": None, "answer_state": "answered", "quality": None,
            "hit": True, "citation_count": 1, "contract_support_rate": 1.0,
            "evidence_hash": f"hash-{index + 1}", "status": 200, "ms": 100,
        })
        reviews.append({"id": item_id, "status": "reviewed", "run_id": "run-1"})
    result["question_count"] = 20
    result["retrieval"]["items"] = retrieval_items
    result["retrieval"]["success_rate"] = 0.95
    result["retrieval"]["error_rate"] = 0.05
    result["answer"]["items"] = answer_items
    assert evaluate(result, reviews)["ok"] is True


def test_gate_passes_only_with_complete_review_evidence():
    assert evaluate(passing_result(), reviewed()) == {"ok": True, "failures": []}


def test_gate_does_not_mark_pending_reviews_complete():
    review = reviewed()
    review[0]["status"] = "pending"
    before = [item.copy() for item in review]
    evaluate(passing_result(), review)
    assert review == before


def test_gate_rejects_incomplete_review_coverage():
    result = passing_result()
    result["question_count"] = 2
    outcome = evaluate(result, reviewed())
    assert "2 retrieval sample count does not match question_count" not in outcome["failures"]
    assert "answer sample count does not match question_count" in outcome["failures"]
    assert "retrieval sample count does not match question_count" in outcome["failures"]


@pytest.mark.parametrize("field,value", [
    ("hit_rate", math.nan), ("mrr", math.inf), ("ndcg", "bad"), ("p95_ms", -1),
])
def test_gate_rejects_nonfinite_non_numeric_and_out_of_range_metrics(field, value):
    result = passing_result()
    result["retrieval"][field] = value
    assert any("invalid" in failure for failure in evaluate(result, reviewed())["failures"])


def test_gate_requires_answer_hit_keyword_and_explicit_no_answer_quality():
    result = passing_result()
    result["answer"]["hit_rate"] = 0.0
    result["answer"]["keyword_rate"] = 0.0
    result["answer"]["items"][0]["keywords"] = []
    outcome = evaluate(result, reviewed())
    assert "answer document hit rate < 90%" in outcome["failures"]
    assert "answer keyword coverage < 100%" in outcome["failures"]

    result = passing_result()
    item = result["answer"]["items"][0]
    item.update(expected_answer_state="no_answer", answer_state="answered", quality=False,
                expected_keywords=[], keywords=[])
    result["answer"]["keyword_rate"] = 0.0
    outcome = evaluate(result, reviewed())
    assert "answer sample q1 expected no_answer" in outcome["failures"]


def test_gate_reconciles_summary_with_raw_samples():
    result = passing_result()
    result["retrieval"]["items"][0].update(status=500, ms=999_999, hit=False, mrr=0.0, ndcg=0.0)
    result["answer"]["items"][0].update(status=500, ms=999_999, hit=False, keywords=[])
    outcome = evaluate(result, reviewed())
    assert outcome["ok"] is False
    assert "retrieval.success_rate does not match samples" in outcome["failures"]
    assert "retrieval.p95_ms does not match samples" in outcome["failures"]
    assert "answer.hit_rate does not match samples" in outcome["failures"]
    assert "answer.keyword_rate does not match samples" in outcome["failures"]


@pytest.mark.parametrize("field,value", [("status", 600), ("ms", math.inf)])
def test_gate_requires_valid_raw_sample_fields(field, value):
    result = passing_result()
    result["retrieval"]["items"][0][field] = value
    outcome = evaluate(result, reviewed())
    assert any("retrieval sample" in failure for failure in outcome["failures"])


def test_gate_rejects_stale_review_even_when_question_id_matches():
    outcome = evaluate(passing_result(), reviewed(run_id="old-run"))
    assert any("manual reviews not bound to current answers" in failure for failure in outcome["failures"])


def test_load_aggregation_counts_errors_429_and_bounded_drops():
    samples = [
        Sample("employee", "warm", 200, 10),
        Sample("employee", "varied", 429, 20),
        Sample("machine", "warm", 500, 30),
    ]
    result = aggregate(samples, dropped=1, duration_s=2,
                       dropped_by_kind={"employee": 1, "machine": 0})
    assert result["requests"] == 4
    assert result["completed"] == 3
    assert result["dropped"] == 1
    assert result["success_rate"] == 0.25
    assert result["error_rate"] == 0.75
    assert result["429_count"] == 1
    assert result["status_counts"] == {"200": 1, "429": 1, "500": 1}
    assert result["employee"]["requests"] == 3
    assert result["machine"]["requests"] == 1
    assert result["warm"]["requests"] == 2
    assert result["varied"]["requests"] == 1
    assert result["successful_rps"] == 0.5
    assert result["completed_rps"] == 1.5


def test_request_schedule_interleaves_employee_and_machine_first_ten():
    specs = [request_spec(index, 0.8) for index in range(10)]
    assert {spec.kind for spec in specs} == {"employee", "machine"}
    assert sum(spec.kind == "employee" for spec in specs) == 8
    assert {spec.variant for spec in specs} == {"warm", "varied"}


class _Response:
    status = 200
    headers = {"X-Cache-Hit": "hit"}

    def __init__(self, body=b'{"cache_hit": true}'):
        self.body = body

    def read(self, limit):
        assert limit == 1_000_001
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_request_auth_headers_cache_observation_and_queue_wait():
    config = LoadConfig("https://example.test", "employee-cookie", "machine-token")
    seen = []

    def opener(request, timeout):
        seen.append((request.headers, timeout))
        return _Response()

    employee = request_one(config, request_spec(0, 1.0), opener=opener,
                           scheduled_at=time.monotonic() - 0.1)
    machine = request_one(config, request_spec(1, 0.0), opener=opener)
    assert employee.cache_hit is True
    assert employee.queue_wait_ms >= 90
    assert seen[0][0]["Cookie"] == "employee-cookie"
    assert "Authorization" not in seen[0][0]
    assert seen[1][0]["Authorization"] == "Bearer machine-token"
    assert "Cookie" not in seen[1][0]


def test_default_opener_does_not_follow_redirects():
    assert _NoRedirect().redirect_request(None, None, 302, "found", {}, "https://evil.test") is None


def test_load_config_requires_separate_auth_and_finite_limits(monkeypatch):
    monkeypatch.delenv("RAG_BASE_URL", raising=False)
    monkeypatch.delenv("RAG_EMPLOYEE_SESSION_COOKIE", raising=False)
    monkeypatch.delenv("RAG_MACHINE_BEARER_TOKEN", raising=False)
    with pytest.raises(ValueError, match="RAG_BASE_URL"):
        LoadConfig.from_env()

    config = LoadConfig("https://example.test", "employee", "machine", concurrency=0)
    with pytest.raises(ValueError, match="must be finite, positive"):
        config.validate()

    config = LoadConfig("https://example.test", "employee", "machine", duration_s=math.inf)
    with pytest.raises(ValueError, match="must be finite"):
        config.validate()

    config = LoadConfig("https://user:pass@example.test", "employee", "machine")
    with pytest.raises(ValueError, match="credentials"):
        config.validate()

    monkeypatch.setenv("RAG_BASE_URL", "https://example.test")
    monkeypatch.setenv("RAG_EMPLOYEE_SESSION_COOKIE", "employee")
    monkeypatch.setenv("RAG_MACHINE_BEARER_TOKEN", "machine")
    config = LoadConfig.from_env()
    assert config.employee_session_cookie == "employee"
    assert config.machine_bearer_token == "machine"
