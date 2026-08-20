import argparse
import hashlib
import json
from pathlib import Path

import httpx


ROOT = Path(__file__).parent / "corpus"


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize the bundled evaluation corpus.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--knowledge-base")
    args = parser.parse_args()

    manifest = json.loads((ROOT / "manifest.json").read_text())
    knowledge_base = args.knowledge_base or manifest["knowledge_base"]
    results = []
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=120) as client:
        for item in manifest["documents"]:
            path = ROOT / item["file"]
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != item["sha256"]:
                raise SystemExit(f"Checksum mismatch: {item['file']}")
            with path.open("rb") as source:
                response = client.post(
                    "/api/v1/documents",
                    data={"knowledge_base": knowledge_base},
                    files={"file": (path.name, source, "text/plain")},
                )
            response.raise_for_status()
            results.append({"file": path.name, **response.json()})

    print(json.dumps({"knowledge_base": knowledge_base, "documents": results}, ensure_ascii=False))


if __name__ == "__main__":
    main()
