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

Before returning cached or generated results, the API rechecks current identity roles, document grants/current ready versions and approved alias bindings. Unrelated tenant ingestion does not reject an otherwise valid long-running answer; its cache entry keeps the original knowledge-revision key. Revoked evidence returns 409, and unavailable authorization state fails closed.

Uploads hold one of two per-process slots through bounded reading and persistence. This protects the API's 2Gi memory limit; it is not verification of the design's 20 concurrent uploads. Multipart requests can still consume temporary disk while waiting, so ingress body/rate limits and disk alerts are deployment prerequisites.

The current PostgreSQL-to-Qdrant permission filter supports at most 10,000 current ready document versions for one tenant/role scope; overflow returns 503, never a silently empty answer. Approved alias lookup similarly stops at 5,000 rows. These are explicit implementation ceilings, not limits implied by 1500 employee accounts. A larger corpus needs a different visibility/filter representation and measured migration before raising the constants.

## Upgrade and recovery

1. Before opening traffic, back up PostgreSQL and original objects, apply `alembic upgrade head`, and restart the API/workers on the same release. Parallel migrations are serialized with a PostgreSQL advisory lock.
2. For an existing installation, use the administrator's `POST /api/v1/admin/documents/reindex-all` per tenant to create version-scoped jobs/events. Legacy document-id-only Kafka messages are intentionally dead-lettered, and old Qdrant points without `document_version_id` are not visible. If a document lacks its current `document_versions` row or source object, repair that metadata/source first; bulk reindex cannot invent it.
3. Compare the queued count with eligible documents, then wait for current versions to become ready. Reconcile failed jobs, unpublished outbox rows, database dead letters and Kafka backlog before opening traffic. Reindexing temporarily hides affected documents; schedule a maintenance window. Test a sample of permitted and forbidden role/tenant queries.
4. Retry indexing failures through the administrator's reindex endpoint after fixing their cause. For a document stuck deleting after exhausted cleanup, repeat `DELETE /api/v1/documents/{id}`: it requeues only dead-letter cleanup jobs and does not replace active attempts. Do not replay raw legacy messages or mark jobs successful manually. Deletion uses a durable tombstone before vector cleanup; source objects are retained under the separate retention policy. Dead-letter cleanup of superseded versions on otherwise live documents requires operator reconciliation; no generic dead-letter replay UI is provided.

Application manifests now run two API and two worker replicas with disruption budgets and preferred node spreading. This does not make external PostgreSQL, Redis, Kafka, MinIO, Qdrant or the model gateway highly available. Worker throughput also depends on the configured topic's partition count.

## Reproducible local verification

Install the locked test environment with `uv sync --extra dev --locked`. Run `python -m pytest tests` against synthetic settings, never a production `.env`. Real integration tests are opt-in through `TEST_DATABASE_URL`, `TEST_REDIS_URL`, `TEST_QDRANT_URL`, `TEST_S3_URL`, `TEST_S3_ACCESS_KEY`, `TEST_S3_SECRET_KEY`, and `TEST_KAFKA_BOOTSTRAP`; point these only at disposable services. They create and remove uniquely named test resources.

The pipeline test exercises real PostgreSQL, Kafka, MinIO and Qdrant: upload, committed outbox, broker delivery, duplicate handling, indexing, cross-role/cross-tenant denial, tombstone and physical vector cleanup. Embeddings are deterministic test vectors. It establishes component integration, not model answer quality, production throughput, or disaster recovery.

Verification snapshot (2026-09-10): 133 tests passed with the disposable services enabled, no skips, and 338 warnings. Locked dependency checks, Python compilation, Compose validation and Kubernetes rendering passed. Admin build and browser checks confirmed pagination across 125 synthetic documents and a denied-preview error without exposing content. No production changes, live model benchmark or 1500-person load sign-off were performed.

## Required release evidence

- Authorization: cross-tenant, cross-role, empty roles, revoked access, deleted content, cached citations, and independently provisioned machine identities.
- Recovery: interrupted upload/publish/index, duplicate delivery, obsolete versions, cancellation/deletion races, worker restart, and exhausted retries.
- Load: sustained mixed employee/machine requests, separate cold/warm runs, concurrent ingestion, representative corpus, error/429 rates, p95/p99, model throughput, database waits, and Kafka backlog recovery.
- Operations: redundant application replicas, managed backing-service availability, backup restoration, model gateway budgets, and migration rollout verification.

Request logs now include a generated request ID, route template, status and duration without query strings or source text. Full stage timing, model token accounting, backing-service dashboards and alert delivery still require operational instrumentation and acceptance.

The local evaluation thresholds in `rag-evaluation.md` are not proof of the stricter production targets in `rag-system-design.md`. In particular, the current JSON answer API does not stream first tokens; its end-to-end latency cannot establish the design's first-token target. No 1500-person capacity sign-off is valid until representative measurements and operational recovery evidence exist.
