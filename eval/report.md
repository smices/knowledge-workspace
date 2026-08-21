# RAG Benchmark Report

Base URL: `http://127.0.0.1:8000`
Questions: 16

## Retrieval
- Success: 100.0%
- Document hit: 81.2%
- MRR / NDCG: 0.781 / 0.789
- p50/p95: 147.6 / 325.1 ms

## Answer
- Success: 100.0%
- Document hit: 81.2%
- Citation: 81.2%
- Evidence contract support: 81.2%
- Keyword: 75.0%
- p50/p95: 30996.4 / 44601.3 ms

## Concurrent Retrieval
- Workers: 4
- Success: 100.0%
- p95: 448.8 ms

## Failures
- `hl-01` quality miss
- `hl-02` quality miss
- `hl-04` quality miss
- `hl-01` quality miss
- `hl-02` quality miss
- `hl-04` quality miss
