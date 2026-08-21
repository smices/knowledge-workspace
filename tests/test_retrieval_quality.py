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
    assert _focus_parts("请归纳贾宝玉与贾母、王夫人、林黛玉和薛宝钗的关系。") == [
        "賈寶玉", "賈母", "王夫人", "林黛玉", "薛寶釵",
    ]
    assert _focus_parts("诸葛亮的人物关系图：请列出他的主公、主要盟友和主要对手，每类最多三人。") == ["諸葛亮"]
    assert _focus_parts("孙悟空为什么大闹天宫？请归纳主要原因。") == ["孫悟空"]
    assert _focus_parts("林冲为什么被发配？请归纳陷害、定罪和结果。") == ["林沖"]
    assert _focus_parts("宋江为什么接受招安？请总结主要动机和结果。") == ["宋江"]


def test_low_lexical_relevance_is_rejected():
    points = [SimpleNamespace(payload={"content": "刘备"}, score=0.95)]
    assert _rank_points(points, "刘备和诸葛亮是什么关系？", 5) == []


def test_single_entity_instruction_does_not_lower_lexical_relevance():
    points = [SimpleNamespace(payload={"content": "诸葛亮辅佐刘备"}, score=0.95)]
    assert len(_rank_points(points, "诸葛亮的人物关系图：请列出他的主公。", 5)) == 1
    points = [SimpleNamespace(payload={"content": "唐僧与孙悟空师徒同行"}, score=0.95)]
    assert len(_rank_points(points, "唐僧取经团队有哪些成员？", 5)) == 1
