# RAG Benchmark Report

Base URL: `http://127.0.0.1:8000`
Questions: 16

## Retrieval
- Success: 100.0%
- Document hit: 100.0%
- MRR / NDCG: 0.969 / 0.977
- p50/p95: 854.2 / 1320.1 ms

## Answer
- Success: 100.0%
- Document hit: 100.0%
- Citation: 100.0%
- Evidence contract support: 100.0%
- Keyword: 93.8%
- p50/p95: 22929.7 / 72617.4 ms

## Concurrent Retrieval
- Workers: 4
- Success: 100.0%
- p95: 1574.8 ms

## Failures
- none
