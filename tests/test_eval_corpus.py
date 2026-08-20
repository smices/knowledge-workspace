import hashlib
import json
from pathlib import Path


def test_evaluation_corpus_is_complete_and_unchanged():
    root = Path(__file__).parents[1] / "eval" / "corpus"
    manifest = json.loads((root / "manifest.json").read_text())

    assert manifest["knowledge_base"] == "测试资料"
    assert {item["file"] for item in manifest["documents"]} == {
        "三国演义.txt", "西游记.txt", "水浒传.txt", "红楼梦.txt",
    }
    for item in manifest["documents"]:
        assert hashlib.sha256((root / item["file"]).read_bytes()).hexdigest() == item["sha256"]
