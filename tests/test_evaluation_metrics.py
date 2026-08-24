from eval.benchmark import answer_quality, ranking_metrics


def test_ranking_metrics_rewards_earlier_expected_document():
    metrics = ranking_metrics([{"title": "other"}, {"title": "expected"}], {"expected"})
    assert metrics == {"hit": True, "mrr": 0.5, "ndcg": 1 / __import__("math").log2(3)}


def test_expected_no_answer_requires_insufficient_contract():
    assert answer_quality("no_answer", "no_answer", [{"support": "insufficient"}]) is True
    assert answer_quality("no_answer", "answered", [{"support": "supported"}]) is False
