import argparse
import hashlib
import json
import math
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

if __package__:
    from .gate import evaluate, load_review
else:
    from gate import evaluate, load_review

ROOT = Path(__file__).parent
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirect)


def _body(response):
    try:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            return {}
        return json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def call(base, path, payload, auth_token=None, timeout=180, opener=None):
    headers = {'Content-Type': 'application/json'}
    if auth_token:
        headers['Authorization'] = f'Bearer {auth_token}'
    request = Request(base.rstrip('/') + path, data=json.dumps(payload, ensure_ascii=False).encode(), headers=headers)
    started = time.perf_counter()
    try:
        with (opener or _NO_REDIRECT_OPENER).open(request, timeout=timeout) as response:
            return response.status, _body(response), (time.perf_counter() - started) * 1000, None
    except HTTPError as exc:
        return exc.code, _body(exc), (time.perf_counter() - started) * 1000, f'HTTP {exc.code}'
    except Exception as exc:
        return 0, {}, (time.perf_counter() - started) * 1000, type(exc).__name__


def percentile(values, p):
    if not 0 <= p <= 1:
        raise ValueError('p must be between 0 and 1')
    if not values:
        return 0
    values = sorted(values)
    return values[min(len(values) - 1, max(0, math.ceil(len(values) * p) - 1))]


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else 0.0


def summarize_samples(retrieval, answers):
    """Build the gated aggregates from the raw per-question samples.

    Keeping this calculation next to benchmark generation gives the gate one
    definition of success, latency, ranking, and answer-quality metrics.  The
    gate imports this lazily so benchmark's existing gate import remains
    cycle-free.
    """
    retrieval = list(retrieval)
    answers = list(answers)
    retrieval_times = [item['ms'] for item in retrieval]
    answer_times = [item['ms'] for item in answers]
    keyword_items = [item for item in answers if item.get('expected_keywords')]
    retrieval_ok = sum(item['status'] == 200 for item in retrieval)
    answer_ok = sum(item['status'] == 200 for item in answers)
    return {
        'retrieval': {
            'success_rate': _ratio(retrieval_ok, len(retrieval)),
            'error_rate': _ratio(len(retrieval) - retrieval_ok, len(retrieval)),
            'p50_ms': percentile(retrieval_times, .5),
            'p95_ms': percentile(retrieval_times, .95),
            'p99_ms': percentile(retrieval_times, .99),
            'hit_rate': _ratio(sum(item['hit'] for item in retrieval), len(retrieval)),
            'mrr': _ratio(sum(item['mrr'] for item in retrieval), len(retrieval)),
            'ndcg': _ratio(sum(item['ndcg'] for item in retrieval), len(retrieval)),
        },
        'answer': {
            'success_rate': _ratio(answer_ok, len(answers)),
            'error_rate': _ratio(len(answers) - answer_ok, len(answers)),
            'p50_ms': percentile(answer_times, .5),
            'p95_ms': percentile(answer_times, .95),
            'p99_ms': percentile(answer_times, .99),
            'hit_rate': _ratio(sum(item['hit'] for item in answers), len(answers)),
            'citation_rate': _ratio(sum(item['citation_count'] > 0 for item in answers), len(answers)),
            'keyword_rate': _ratio(sum(bool(item['keywords']) for item in keyword_items), len(keyword_items)),
            'contract_support_rate': _ratio(
                sum(item['contract_support_rate'] for item in answers), len(answers)
            ),
        },
    }


def ranking_metrics(results, expected_titles):
    expected_titles = set(expected_titles)
    seen = set()
    ranks = []
    for index, item in enumerate(results):
        title = item.get('title')
        if title in expected_titles and title not in seen:
            seen.add(title)
            ranks.append(index + 1)
    if not ranks:
        return {'hit': False, 'mrr': 0.0, 'ndcg': 0.0}
    dcg = sum(1 / math.log2(rank + 1) for rank in ranks)
    ideal_dcg = sum(1 / math.log2(rank + 1) for rank in range(1, min(len(results), len(expected_titles)) + 1))
    return {'hit': True, 'mrr': 1 / ranks[0], 'ndcg': dcg / ideal_dcg if ideal_dcg else 0.0}


def answer_quality(expected_state, actual_state, contract):
    if not expected_state:
        return None
    if actual_state != expected_state:
        return False
    if expected_state == 'no_answer':
        return bool(contract) and all(item.get('support') == 'insufficient' for item in contract)
    return True


def evidence_hash(answer_state, answer, citations, contract):
    payload = json.dumps({"answer_state": answer_state, "answer": answer,
                          "citations": citations, "evidence_contract": contract}, ensure_ascii=False,
                         sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(payload).hexdigest()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:18024')
    parser.add_argument('--check', action='store_true', help='fail when the documented quality floor regresses')
    args = parser.parse_args()
    questions = [json.loads(line) for line in (ROOT / 'questions.jsonl').read_text().splitlines() if line.strip()]
    run_id = uuid.uuid4().hex
    retrieval, answers = [], []
    for item in questions:
        status, data, ms, error = call(args.base_url, '/api/v1/retrieval/search', {'query': item['question'], 'limit': 8})
        results = data.get('results', [])
        metrics = ranking_metrics(results, set(item['expected_titles']))
        titles = {r.get('title') for r in results}
        retrieval.append({**item, 'status': status, 'ms': round(ms, 1), 'error': error, **metrics, 'titles': sorted(titles), 'result_count': len(results)})
        status, data, ms, error = call(args.base_url, '/api/v1/rag/answer', {'query': item['question'], 'limit': 3, 'temperature': 0})
        text = data.get('answer', '')
        citations = data.get('citations', [])
        contract = data.get('evidence_contract', [])
        claims = [entry for entry in contract if entry.get('claim')]
        expected_state = item.get('expected_answer_state')
        quality = answer_quality(expected_state, data.get('answer_state'), claims)
        support_rate = (1.0 if quality else 0.0) if quality is not None else (sum(entry.get('support') == 'supported' for entry in claims) / len(claims) if claims else 0.0)
        matched_keywords = [k for k in item.get('expected_keywords', []) if k in text]
        actual_state = data.get('answer_state')
        answers.append({**item, 'status': status, 'ms': round(ms, 1), 'error': error,
                        'hit': quality if quality is not None else any(c.get('title') in item['expected_titles'] for c in citations),
                        'keywords': matched_keywords, 'citation_count': len(citations),
                        'contract_support_rate': support_rate, 'answer_state': actual_state,
                        'quality': quality, 'citations': citations, 'evidence_contract': contract,
                        'evidence_hash': evidence_hash(actual_state, text, citations, contract), 'answer': text})
    concurrent_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(call, args.base_url, '/api/v1/retrieval/search', {'query': q['question'], 'limit': 8}) for q in questions]
        concurrent_results = [f.result() for f in as_completed(futures)]
    concurrent_duration = time.perf_counter() - concurrent_started
    summaries = summarize_samples(retrieval, answers)
    report = {
        'base_url': args.base_url, 'run_id': run_id, 'question_count': len(questions),
        'retrieval': {**summaries['retrieval'], 'items': retrieval},
        'answer': {**summaries['answer'], 'items': answers},
        'concurrent_retrieval': {'workers': 4, 'requests': len(concurrent_results), 'duration_s': round(concurrent_duration, 3), 'completed_rps': _ratio(len(concurrent_results), concurrent_duration), 'successful_rps': _ratio(sum(x[0] == 200 for x in concurrent_results), concurrent_duration), 'throughput_rps': _ratio(len(concurrent_results), concurrent_duration), 'success_rate': _ratio(sum(x[0] == 200 for x in concurrent_results), len(concurrent_results)), 'error_rate': _ratio(sum(x[0] != 200 for x in concurrent_results), len(concurrent_results)), 'p95_ms': percentile([x[2] for x in concurrent_results], .95), 'p99_ms': percentile([x[2] for x in concurrent_results], .99)},
    }
    (ROOT / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    lines = ['# RAG Benchmark Report', '', f"Base URL: `{args.base_url}`", f"Questions: {len(questions)}", '', '## Retrieval', f"- Success: {report['retrieval']['success_rate']:.1%}", f"- Document hit: {report['retrieval']['hit_rate']:.1%}", f"- MRR / NDCG: {report['retrieval']['mrr']:.3f} / {report['retrieval']['ndcg']:.3f}", f"- p50/p95: {report['retrieval']['p50_ms']:.1f} / {report['retrieval']['p95_ms']:.1f} ms", '', '## Answer', f"- Success: {report['answer']['success_rate']:.1%}", f"- Document hit: {report['answer']['hit_rate']:.1%}", f"- Citation: {report['answer']['citation_rate']:.1%}", f"- Evidence contract support: {report['answer']['contract_support_rate']:.1%}", f"- Keyword: {report['answer']['keyword_rate']:.1%}", f"- p50/p95: {report['answer']['p50_ms']:.1f} / {report['answer']['p95_ms']:.1f} ms", '', '## Concurrent Retrieval', f"- Workers: 4", f"- Success: {report['concurrent_retrieval']['success_rate']:.1%}", f"- p95: {report['concurrent_retrieval']['p95_ms']:.1f} ms", '', '## Failures']
    failures = [x for group in (retrieval, answers) for x in group if x['status'] != 200 or not x['hit']]
    lines += [f"- `{x['id']}` {x.get('error') or 'quality miss'}" for x in failures] or ['- none']
    (ROOT / 'report.md').write_text('\n'.join(lines) + '\n')
    if args.check:
        try:
            review = load_review()
        except FileNotFoundError:
            review = None
        outcome = evaluate(report, review)
        if not outcome['ok']:
            raise SystemExit('RAG evaluation gate failed: ' + '; '.join(outcome['failures']))
    print(json.dumps({k: v for k, v in report.items() if k != 'items'}, ensure_ascii=False, indent=2))

if __name__ == '__main__': main()
