import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent

def main():
    result = json.loads((ROOT / 'results.json').read_text())
    review = [json.loads(line) for line in (ROOT / 'review.jsonl').read_text().splitlines() if line.strip()]
    failures = []
    if result['retrieval']['success_rate'] < .95: failures.append('retrieval success rate < 95%')
    if result['retrieval']['hit_rate'] < .90: failures.append('retrieval document hit rate < 90%')
    if result['answer']['success_rate'] < .95: failures.append('answer success rate < 95%')
    if result['answer']['citation_rate'] < 1.0: failures.append('citation coverage < 100%')
    pending = sum(item.get('status') != 'reviewed' for item in review)
    if pending: failures.append(f'{pending} manual reviews pending')
    print(json.dumps({'ok': not failures, 'failures': failures}, ensure_ascii=False, indent=2))
    raise SystemExit(1 if failures else 0)

if __name__ == '__main__': main()
