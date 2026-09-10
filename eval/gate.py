"""Apply the documented offline evaluation and manual-review gate."""

import json
import math
from pathlib import Path

ROOT = Path(__file__).parent
ERROR_RATE_TOLERANCE = 1e-9

METRIC_RULES = (
    ("retrieval.success_rate", 0.95, "min", "retrieval success rate < 95%"),
    ("retrieval.p95_ms", 2_000, "max", "retrieval p95 > 2 seconds"),
    ("retrieval.hit_rate", 0.90, "min", "retrieval document hit rate < 90%"),
    ("retrieval.mrr", 0.75, "min", "retrieval MRR < 0.75"),
    ("retrieval.ndcg", 0.75, "min", "retrieval NDCG < 0.75"),
    ("answer.success_rate", 0.95, "min", "answer success rate < 95%"),
    ("answer.p95_ms", 60_000, "max", "answer p95 > 60 seconds"),
    ("answer.hit_rate", 0.90, "min", "answer document hit rate < 90%"),
    ("answer.keyword_rate", 1.0, "min", "answer keyword coverage < 100%"),
    ("answer.citation_rate", 1.0, "min", "citation coverage < 100%"),
    ("answer.contract_support_rate", 0.85, "min", "direct evidence support < 85%"),
)
RATE_FIELDS = {
    "success_rate", "error_rate", "hit_rate", "mrr", "ndcg", "keyword_rate",
    "citation_rate", "contract_support_rate", "429_rate",
}
VALID_METRIC_FIELDS = RATE_FIELDS | {
    "p50_ms", "p95_ms", "p99_ms", "request_p50_ms", "request_p95_ms",
    "request_p99_ms", "queue_wait_p95_ms", "throughput_rps", "completed_rps",
    "successful_rps", "cache_hit_rate",
}


def load_review(path: Path = ROOT / "review.jsonl") -> list[dict]:
    """Load review evidence without changing it or inferring completion."""
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _value(result: dict, dotted_name: str):
    value = result
    for part in dotted_name.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _valid_metric(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return False
    field = name.rsplit(".", 1)[-1]
    if field in RATE_FIELDS:
        return 0 <= value <= 1
    if field.endswith("_ms"):
        return value >= 0
    if field.endswith("_rps") or field.endswith("_count"):
        return value >= 0
    return True


def _valid_sample_status(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 599


def _valid_sample_ms(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _validate_items(result, failures):
    question_count = result.get("question_count")
    if isinstance(question_count, bool) or not isinstance(question_count, int) or question_count <= 0:
        failures.append("question_count must be a positive integer")
        return [], []
    retrieval_section = result.get("retrieval") if isinstance(result.get("retrieval"), dict) else {}
    answer_section = result.get("answer") if isinstance(result.get("answer"), dict) else {}
    retrieval_items = retrieval_section.get("items")
    answer_items = answer_section.get("items")
    if not isinstance(retrieval_items, list) or len(retrieval_items) != question_count:
        failures.append("retrieval sample count does not match question_count")
        retrieval_items = []
    if not isinstance(answer_items, list) or len(answer_items) != question_count:
        failures.append("answer sample count does not match question_count")
        answer_items = []

    def ids(items, section):
        values = []
        for item in items:
            item_id = item.get("id") if isinstance(item, dict) else None
            if not isinstance(item_id, str) or not item_id.strip():
                failures.append(f"{section} sample id missing")
            else:
                values.append(item_id)
        if len(values) != len(set(values)):
            failures.append(f"duplicate {section} sample ids")
        return set(values)

    retrieval_ids = ids(retrieval_items, "retrieval")
    answer_ids = ids(answer_items, "answer")
    if retrieval_ids != answer_ids:
        failures.append("retrieval and answer sample ids differ")

    for item in retrieval_items:
        if not isinstance(item, dict):
            failures.append("retrieval sample quality is missing")
            continue
        if not _valid_sample_status(item.get("status")):
            failures.append("retrieval sample status is invalid")
        if not _valid_sample_ms(item.get("ms")):
            failures.append("retrieval sample latency is invalid")
        if not isinstance(item.get("hit"), bool):
            failures.append("retrieval sample quality is missing")
        if (
            not _valid_metric("retrieval.mrr", item.get("mrr"))
            or not _valid_metric("retrieval.ndcg", item.get("ndcg"))
        ):
            failures.append("retrieval sample ranking metrics are invalid")
        expected_titles = item.get("expected_titles") if isinstance(item, dict) else None
        if not isinstance(expected_titles, list) or not expected_titles:
            failures.append("retrieval sample expected_titles missing")

    for item in answer_items:
        if not isinstance(item, dict):
            failures.append("answer sample quality is missing")
            continue
        if not _valid_sample_status(item.get("status")):
            failures.append("answer sample status is invalid")
        if not _valid_sample_ms(item.get("ms")):
            failures.append("answer sample latency is invalid")
        if not isinstance(item.get("hit"), bool):
            failures.append("answer sample hit is missing")
        if not isinstance(item.get("answer_state"), str) or not item.get("answer_state"):
            failures.append("answer sample answer_state is missing")
        expected_state = item.get("expected_answer_state")
        expected_keywords = item.get("expected_keywords")
        keywords = item.get("keywords")
        if not isinstance(expected_keywords, list) or not isinstance(keywords, list):
            failures.append("answer sample keyword evidence is missing")
        elif expected_state != "no_answer" and not expected_keywords:
            failures.append("answer sample expected_keywords missing")
        if expected_state == "no_answer":
            if item.get("answer_state") != "no_answer" or item.get("quality") is not True:
                failures.append(f"answer sample {item.get('id')} expected no_answer")
        elif expected_state == "answered" and item.get("quality") is not True:
            failures.append(f"answer sample {item.get('id')} expected answered")
        elif expected_state is not None and expected_state not in {"answered", "no_answer"}:
            failures.append(f"answer sample {item.get('id')} expected_answer_state invalid")
        if (isinstance(item.get("citation_count"), bool)
                or not isinstance(item.get("citation_count"), int)
                or item.get("citation_count") < 0
                or not _valid_metric("answer.contract_support_rate", item.get("contract_support_rate"))):
            failures.append("answer sample quality metrics are invalid")
    return retrieval_items, answer_items


def _reconcile_summary(result, retrieval_items, answer_items, failures):
    """Reject reports whose aggregates do not describe their raw samples."""
    if not retrieval_items or not answer_items:
        return
    try:
        from .benchmark import summarize_samples
    except ImportError:  # pragma: no cover - supports running this file directly
        from benchmark import summarize_samples
    try:
        expected = summarize_samples(retrieval_items, answer_items)
    except (KeyError, TypeError, ZeroDivisionError):
        # The item validator already reported the malformed raw sample.  Do
        # not turn a bad report into an unhandled gate exception as well.
        return
    for section, summary in expected.items():
        actual = result.get(section)
        if not isinstance(actual, dict):
            continue
        for field, expected_value in summary.items():
            if field not in actual or not _valid_metric(f"{section}.{field}", actual[field]):
                continue
            if not math.isclose(actual[field], expected_value, rel_tol=1e-9, abs_tol=1e-9):
                failures.append(f"{section}.{field} does not match samples")


def _review_matches(item, result, answer_by_id):
    answer = answer_by_id.get(item.get("id"), {})
    run_id = result.get("run_id")
    if isinstance(run_id, str) and run_id and item.get("run_id") == run_id:
        return True
    review_hash = item.get("evidence_hash") or item.get("answer_evidence_hash")
    evidence_hash = answer.get("evidence_hash")
    return isinstance(review_hash, str) and bool(review_hash) and review_hash == evidence_hash


def gate_failures(result: dict, review: list[dict] | None) -> list[str]:
    """Return every failed metric or review condition; never mutates review state."""
    failures = []
    for name, threshold, operator, message in METRIC_RULES:
        value = _value(result, name)
        if value is None:
            failures.append(f"{name} missing")
        elif not _valid_metric(name, value):
            failures.append(f"{name} invalid")
        elif (operator == "min" and value < threshold) or (operator == "max" and value > threshold):
            failures.append(message)

    for section in ("retrieval", "answer"):
        metrics = result.get(section)
        if isinstance(metrics, dict):
            for field, value in metrics.items():
                if field in VALID_METRIC_FIELDS and value is not None and not _valid_metric(
                    f"{section}.{field}", value
                ):
                    failures.append(f"{section}.{field} invalid")

    for section in ("retrieval", "answer"):
        success_rate = _value(result, f"{section}.success_rate")
        error_rate = _value(result, f"{section}.error_rate")
        if error_rate is not None and _valid_metric(f"{section}.error_rate", error_rate):
            if error_rate > 0.05 + ERROR_RATE_TOLERANCE:
                failures.append(f"{section} error rate > 5%")
        elif error_rate is None and _valid_metric(f"{section}.success_rate", success_rate):
            # The success-rate threshold is authoritative when no explicit error rate exists.
            if success_rate < 0.95 - ERROR_RATE_TOLERANCE:
                failures.append(f"{section} error rate > 5%")
        elif error_rate is not None:
            failures.append(f"{section} error rate invalid")

    retrieval_items, answer_items = _validate_items(result, failures)
    _reconcile_summary(result, retrieval_items, answer_items, failures)
    answer_by_id = {item.get("id"): item for item in answer_items if isinstance(item, dict)}
    if review is None:
        failures.append("manual review evidence missing")
    elif not review:
        failures.append("manual review evidence empty")
    else:
        pending = sum(item.get("status") != "reviewed" for item in review if isinstance(item, dict))
        if pending:
            failures.append(f"{pending} manual reviews pending")
        review_ids = [item.get("id") for item in review if isinstance(item, dict)]
        if len(review_ids) != len(set(review_ids)):
            failures.append("duplicate manual review ids")
        expected_ids = set(answer_by_id)
        actual_ids = set(review_ids)
        if expected_ids - actual_ids:
            failures.append(f"{len(expected_ids - actual_ids)} manual reviews missing")
        if actual_ids - expected_ids:
            failures.append("manual review ids do not match current run")
        unbound = [item.get("id") for item in review if isinstance(item, dict)
                   and not _review_matches(item, result, answer_by_id)]
        if unbound:
            failures.append(f"{len(unbound)} manual reviews not bound to current answers")
    return failures


def evaluate(result: dict, review: list[dict] | None) -> dict:
    failures = gate_failures(result, review)
    return {"ok": not failures, "failures": failures}


def main() -> None:
    result = json.loads((ROOT / "results.json").read_text())
    try:
        review = load_review()
    except FileNotFoundError:
        review = None
    outcome = evaluate(result, review)
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    raise SystemExit(0 if outcome["ok"] else 1)


if __name__ == "__main__":
    main()
