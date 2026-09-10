from math import log2

from eval.benchmark import answer_quality, percentile, ranking_metrics


def test_ranking_metrics_rewards_earlier_expected_document():
    metrics = ranking_metrics([{"title": "other"}, {"title": "expected"}], {"expected"})
    assert metrics == {"hit": True, "mrr": 0.5, "ndcg": 1 / log2(3)}


def test_ndcg_uses_all_relevant_documents_and_ideal_ranking():
    metrics = ranking_metrics(
        [{"title": "other"}, {"title": "expected-a"}, {"title": "expected-b"}],
        {"expected-a", "expected-b"},
    )
    actual = 1 / log2(3) + 1 / log2(4)
    ideal = 1 + 1 / log2(3)
    assert metrics["ndcg"] == actual / ideal


def test_ranking_metrics_deduplicates_repeated_chunks_from_one_document():
    metrics = ranking_metrics([{"title": "expected"}, {"title": "expected"}], {"expected"})
    assert metrics == {"hit": True, "mrr": 1.0, "ndcg": 1.0}


def test_percentile_uses_nearest_rank_and_handles_empty_values():
    assert percentile([1, 2, 3, 4], 0.95) == 4
    assert percentile([], 0.95) == 0


def test_expected_no_answer_requires_insufficient_contract():
    assert answer_quality("no_answer", "no_answer", [{"support": "insufficient"}]) is True
    assert answer_quality("no_answer", "answered", [{"support": "supported"}]) is False
