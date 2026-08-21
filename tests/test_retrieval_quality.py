from types import SimpleNamespace

from app.vector import _focus_parts, _rank_points


def test_multi_entity_query_requires_cooccurrence_in_one_evidence_chunk():
    points = [
        SimpleNamespace(payload={"content": "刘备三顾茅庐"}, score=0.9),
        SimpleNamespace(payload={"content": "诸葛亮辅佐刘备"}, score=0.8),
        SimpleNamespace(payload={"content": "刘备请诸葛亮出山"}, score=0.7),
    ]
    result = _rank_points(points, "刘备和诸葛亮是什么关系？", 5)
    assert len(result) == 2
    assert all("刘备" in item.payload["content"] and "诸葛亮" in item.payload["content"] for item in result)
    assert _focus_parts("刘备和诸葛亮的关系是什么？") == ["劉備", "諸葛亮"]
    assert _focus_parts("赤壁之战中，诸葛亮和周瑜分别起到了什么作用？") == ["諸葛亮", "周瑜"]
    assert _focus_parts("宋江在梁山的主要角色是什么？他与晁盖是什么关系？") == ["宋江", "晁蓋"]


def test_low_lexical_relevance_is_rejected():
    points = [SimpleNamespace(payload={"content": "刘备"}, score=0.95)]
    assert _rank_points(points, "刘备和诸葛亮是什么关系？", 5) == []
