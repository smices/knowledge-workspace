# RAG Benchmark Report

Base URL: `http://127.0.0.1:8000`
Questions: 16

## Retrieval
- Success: 100.0%
- Document hit: 100.0%
- MRR / NDCG: 0.969 / 0.977
- p50/p95: 490.1 / 1311.1 ms

## Answer
- Success: 100.0%
- Document hit: 100.0%
- Citation: 100.0%
- Evidence contract support: 100.0%
- Keyword: 81.2%
- p50/p95: 23068.5 / 83353.2 ms

## Concurrent Retrieval
- Workers: 4
- Success: 100.0%
- p95: 2075.3 ms

## Failures
- none
