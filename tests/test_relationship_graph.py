from app.main import (answer_state, evidence_contract, graph_is_available,
                      merge_relationships, parse_relationships)


def test_graph_is_hidden_when_answer_lacks_confirmed_evidence():
    assert graph_is_available("## 结论\n资料不足", [{"content": "有检索结果"}]) is False
    assert graph_is_available("## 结论\n诸葛亮是刘备的军师\n## 依据\n[证据 1]", [{"content": "诸葛亮辅佐刘备"}]) is True


def test_parse_relationships_keeps_only_source_bound_edges():
    contexts = [{"content": "刘备三顾茅庐，请诸葛亮出山辅佐。"}]
    edges = parse_relationships(
        '[{"source":"刘备","target":"诸葛亮","relation":"主臣","evidence":1},'
        '{"source":"刘备","target":"关羽","relation":"结义","evidence":1}]',
        contexts,
    )
    assert edges == [{"source": "刘备", "target": "诸葛亮", "label": "主从", "relation_type": "standard", "evidence": 1,
                      "excerpt": "刘备三顾茅庐，请诸葛亮出山辅佐。"}]


def test_parse_relationships_requires_entities_named_in_query():
    contexts = [{"content": "孙悟空与牛王交战，菩萨和玉帝也在相关记载中出现。"}]
    edges = parse_relationships(
        '[{"source":"孙悟空","target":"牛王","relation":"打斗","evidence":1}]',
        contexts,
        query="哈利波特和孙悟空是什么关系？",
    )
    assert edges == []


def test_relationships_normalize_and_merge_evidence():
    edges = merge_relationships([
        {"source": "刘备", "target": "诸葛亮", "label": "主臣", "evidence": 1, "excerpt": "一"},
        {"source": "刘备", "target": "诸葛亮", "label": "主从", "evidence": 2, "excerpt": "二"},
    ])
    assert edges == [{"source": "刘备", "target": "诸葛亮", "label": "主从", "relation_type": "standard", "evidence": [1, 2],
                      "excerpts": ["一", "二"]}]


def test_custom_relation_labels_are_preserved_for_domain_documents():
    contexts = [{"content": "订单服务依赖库存服务，并由平台架构组维护。"}]
    edges = parse_relationships(
        '[{"source":"订单服务","target":"库存服务","relation":"依赖","evidence":1},'
        '{"source":"订单服务","target":"平台架构组","relation":"由...维护","evidence":1}]',
        contexts,
    )
    assert [edge["label"] for edge in edges] == ["依赖", "由...维护"]
    assert all(edge["relation_type"] == "custom" for edge in edges)


def test_answer_contract_distinguishes_states():
    contexts = [{"content": "刘备辅佐诸葛亮", "title": "三国"}]
    answer = "## 结论\n刘备与诸葛亮是主从关系。\n## 归纳\n- 由原文可见。\n## 依据\n[证据 1]"
    assert answer_state(answer, contexts) == "answered"
    assert evidence_contract(answer, contexts)[0]["support"] == "cited"
    assert answer_state("## 结论\n资料不足\n## 依据\n[证据 1]", contexts) == "no_answer"
    assert answer_state("## 结论\n部分可确认，但资料不足\n## 依据\n[证据 1]", contexts) == "partial"
    assert answer_state("## 结论\n证据冲突，无法确认\n## 依据\n[证据 1]", contexts) == "conflict"
