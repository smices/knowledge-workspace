import argparse, json, statistics, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).parent

def call(base, path, payload):
    request = Request(base.rstrip('/') + path, data=json.dumps(payload, ensure_ascii=False).encode(), headers={'Content-Type':'application/json'})
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=180) as response:
            return response.status, json.loads(response.read()), (time.perf_counter() - started) * 1000, None
    except Exception as exc:
        return 0, {}, (time.perf_counter() - started) * 1000, str(exc)

def percentile(values, p):
    if not values: return 0
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p) - 1)]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:18024')
    args = parser.parse_args()
    questions = [json.loads(line) for line in (ROOT / 'questions.jsonl').read_text().splitlines() if line.strip()]
    retrieval, answers = [], []
    for item in questions:
        status, data, ms, error = call(args.base_url, '/api/v1/retrieval/search', {'query': item['question'], 'limit': 8})
        results = data.get('results', [])
        titles = {r.get('title') for r in results}
        retrieval.append({**item, 'status': status, 'ms': round(ms, 1), 'error': error, 'hit': bool(titles & set(item['expected_titles'])), 'titles': sorted(titles), 'result_count': len(results)})
        status, data, ms, error = call(args.base_url, '/api/v1/rag/answer', {'query': item['question'], 'limit': 3, 'temperature': 0})
        text = data.get('answer', '')
        citations = data.get('citations', [])
        answers.append({**item, 'status': status, 'ms': round(ms, 1), 'error': error, 'hit': any(c.get('title') in item['expected_titles'] for c in citations), 'keywords': [k for k in item['expected_keywords'] if k in text], 'citation_count': len(citations), 'answer': text})
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(call, args.base_url, '/api/v1/retrieval/search', {'query': q['question'], 'limit': 8}) for q in questions]
        concurrent_results = [f.result() for f in as_completed(futures)]
    retrieval_ok = sum(x['status'] == 200 for x in retrieval)
    answer_ok = sum(x['status'] == 200 for x in answers)
    report = {
        'base_url': args.base_url, 'question_count': len(questions),
        'retrieval': {'success_rate': retrieval_ok / len(retrieval), 'p50_ms': percentile([x['ms'] for x in retrieval], .5), 'p95_ms': percentile([x['ms'] for x in retrieval], .95), 'hit_rate': sum(x['hit'] for x in retrieval) / len(retrieval), 'items': retrieval},
        'answer': {'success_rate': answer_ok / len(answers), 'p50_ms': percentile([x['ms'] for x in answers], .5), 'p95_ms': percentile([x['ms'] for x in answers], .95), 'hit_rate': sum(x['hit'] for x in answers) / len(answers), 'citation_rate': sum(x['citation_count'] > 0 for x in answers) / len(answers), 'keyword_rate': sum(bool(x['keywords']) for x in answers) / len(answers), 'items': answers},
        'concurrent_retrieval': {'workers': 4, 'success_rate': sum(x[0] == 200 for x in concurrent_results) / len(concurrent_results), 'p95_ms': percentile([x[2] for x in concurrent_results], .95)},
    }
    (ROOT / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    lines = ['# RAG Benchmark Report', '', f"Base URL: `{args.base_url}`", f"Questions: {len(questions)}", '', '## Retrieval', f"- Success: {report['retrieval']['success_rate']:.1%}", f"- Document hit: {report['retrieval']['hit_rate']:.1%}", f"- p50/p95: {report['retrieval']['p50_ms']:.1f} / {report['retrieval']['p95_ms']:.1f} ms", '', '## Answer', f"- Success: {report['answer']['success_rate']:.1%}", f"- Document hit: {report['answer']['hit_rate']:.1%}", f"- Citation: {report['answer']['citation_rate']:.1%}", f"- Keyword: {report['answer']['keyword_rate']:.1%}", f"- p50/p95: {report['answer']['p50_ms']:.1f} / {report['answer']['p95_ms']:.1f} ms", '', '## Concurrent Retrieval', f"- Workers: 4", f"- Success: {report['concurrent_retrieval']['success_rate']:.1%}", f"- p95: {report['concurrent_retrieval']['p95_ms']:.1f} ms", '', '## Failures']
    failures = [x for group in (retrieval, answers) for x in group if x['status'] != 200 or not x['hit']]
    lines += [f"- `{x['id']}` {x.get('error') or 'quality miss'}" for x in failures] or ['- none']
    (ROOT / 'report.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'items'}, ensure_ascii=False, indent=2))

if __name__ == '__main__': main()
