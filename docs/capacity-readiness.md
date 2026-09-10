# Capacity and release acceptance

1500 registered employees is not a throughput measurement. Record peak active users, requests per active user, machine callers and fan-out, corpus size, model throughput, and the required latency before accepting a deployment.

## Query admission

Search, answer and graph share Redis-coordinated budgets across API replicas. Verified service-account bearer callers use the service budget; browser callers use the employee budget. Both global and tenant concurrency apply, alongside a per-subject fixed-minute request allowance. The defaults are protection limits, not measured capacity:

| Setting | Default |
| --- | ---: |
| `EMPLOYEE_CONCURRENCY` | 48 |
| `SERVICE_CONCURRENCY` | 16 |
| `TENANT_EMPLOYEE_CONCURRENCY` | 32 |
| `TENANT_SERVICE_CONCURRENCY` | 8 |
| `EMPLOYEE_REQUESTS_PER_MINUTE` | 30 |
| `SERVICE_REQUESTS_PER_MINUTE` | 60 |
| `REQUEST_TIMEOUT_SECONDS` | 90 |
| `MODEL_TIMEOUT_SECONDS` | 60 |
| `DEPENDENCY_TIMEOUT_SECONDS` | 5 |

Budget exhaustion returns 429 with `Retry-After`; Redis admission failure returns 503. Leases renew while work or cancellation cleanup is active and expire after a process crash. Disconnects cancel all three query paths. Synchronous database/cache work retains its lease until cleanup finishes, so cleanup can extend the response deadline. Fixed-minute allowances permit a boundary burst; model gateway limits still need to reflect its real token/request capacity.

Database pool wait, connection establishment, and SQL statements have configured bounds. Redis sockets and model calls also have explicit timeouts. These limits do not provision database, model, or vector capacity.

## Required release evidence

- Authorization: cross-tenant, cross-role, empty roles, revoked access, deleted content, cached citations, and independently provisioned machine identities.
- Recovery: interrupted upload/publish/index, duplicate delivery, obsolete versions, cancellation/deletion races, worker restart, and exhausted retries.
- Load: sustained mixed employee/machine requests, separate cold/warm runs, concurrent ingestion, representative corpus, error/429 rates, p95/p99, model throughput, database waits, and Kafka backlog recovery.
- Operations: redundant application replicas, managed backing-service availability, backup restoration, model gateway budgets, and migration rollout verification.

The local evaluation thresholds in `rag-evaluation.md` are not proof of the stricter production targets in `rag-system-design.md`. In particular, the current JSON answer API does not stream first tokens; its end-to-end latency cannot establish the design's first-token target. No 1500-person capacity sign-off is valid until representative measurements and operational recovery evidence exist.
