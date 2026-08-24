from app.main import (answer_state, direct_only_answer, evidence_contract, graph_is_available,
                      merge_relationships, parse_relationships, direct_pair_support)


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


def test_direct_pair_gate_rejects_indirect_derivation():
    contexts = [{"content": "红孩儿是牛魔王之子；牛魔王与孙悟空曾结为七兄弟。"}]
    answer = "## 结论\n孙悟空和红孩儿是结义兄弟关系 [证据 1]\n\n## 依据\n[证据 1]"
    support = ["indirect"]
    contract = evidence_contract(answer, contexts, "answered", support)
    filtered, state = direct_only_answer(contract)
    assert contract[0]["support"] == "indirect"
    assert state == "no_answer"
    assert "孙悟空和红孩儿是结义兄弟" not in filtered
    assert direct_pair_support("孙悟空和红孩儿是什么关系？", ["孙悟空和红孩儿是结义兄弟关系 [证据 1]"], contexts, []) == [False]


def test_direct_pair_gate_keeps_direct_claims_when_one_is_indirect():
    contexts = [{"content": "刘备请诸葛亮出山辅佐。"}]
    answer = "## 结论\n刘备与诸葛亮是主从关系 [证据 1]\n孙悟空与红孩儿是结义兄弟关系 [证据 1]\n\n## 依据\n[证据 1]"
    contract = evidence_contract(answer, contexts, "answered", [None, "indirect"])
    filtered, state = direct_only_answer(contract)
    assert state == "partial"
    assert "刘备与诸葛亮" in filtered
    assert "孙悟空与红孩儿" not in filtered
    assert direct_pair_support("刘备和诸葛亮是什么关系？", ["刘备与诸葛亮是主从关系 [证据 1]"], contexts, []) == [True]
    assert direct_pair_support("孙悟空的师父是谁？", ["孙悟空的师父是唐僧 [证据 1]"], contexts, []) == [None]
