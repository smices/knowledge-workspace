from eval.benchmark import ranking_metrics


def test_ranking_metrics_rewards_earlier_expected_document():
    metrics = ranking_metrics([{"title": "other"}, {"title": "expected"}], {"expected"})
    assert metrics == {"hit": True, "mrr": 0.5, "ndcg": 1 / __import__("math").log2(3)}
