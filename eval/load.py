"""Bounded, opt-in capacity probe for read-only RAG endpoints."""

import argparse
import json
import math
import os
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


WARM_QUERY = "刘备和诸葛亮是什么关系？"
VARIED_QUERIES = (
    "赤壁之战中诸葛亮和周瑜分别起到了什么作用？",
    "孙悟空为什么大闹天宫？",
    "林冲为什么被发配？",
    "林黛玉为什么进入贾府？",
)
MAX_RATE = 1_000.0
MAX_CONCURRENCY = 256
MAX_DURATION_S = 86_400.0
MAX_TIMEOUT_S = 300.0
MAX_QUEUE_SIZE = 10_000
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirect)


@dataclass(frozen=True)
class LoadConfig:
    base_url: str
    employee_session_cookie: str
    machine_bearer_token: str = ""
    rate: float = 1.0
    concurrency: int = 4
    duration_s: float = 30.0
    timeout_s: float = 10.0
    queue_size: int = 8
    employee_share: float = 0.8
    max_response_bytes: int = 1_000_000

    @classmethod
    def from_env(cls, **overrides):
        base_url = os.environ.get("RAG_BASE_URL", "").strip()
        employee_cookie = (os.environ.get("RAG_EMPLOYEE_SESSION_COOKIE")
                           or os.environ.get("RAG_EMPLOYEE_SESSION", "")).strip()
        machine_token = (os.environ.get("RAG_MACHINE_BEARER_TOKEN")
                         or os.environ.get("RAG_MACHINE_TOKEN", "")).strip()
        if not base_url or not employee_cookie or not machine_token:
            raise ValueError(
                "RAG_BASE_URL, RAG_EMPLOYEE_SESSION_COOKIE, and RAG_MACHINE_BEARER_TOKEN must be set"
            )
        return cls(base_url=base_url, employee_session_cookie=employee_cookie,
                   machine_bearer_token=machine_token, **overrides)

    def validate(self):
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an http(s) URL")
        if parsed.username or parsed.password:
            raise ValueError("base_url must not contain credentials")
        if any("\r" in value or "\n" in value for value in
               (self.employee_session_cookie, self.machine_bearer_token)):
            raise ValueError("authentication values must not contain newlines")
        if (not math.isfinite(self.rate) or self.rate <= 0 or self.rate > MAX_RATE
                or self.concurrency < 1 or self.concurrency > MAX_CONCURRENCY
                or not math.isfinite(self.duration_s) or self.duration_s <= 0
                or self.duration_s > MAX_DURATION_S
                or not math.isfinite(self.timeout_s) or self.timeout_s <= 0
                or self.timeout_s > MAX_TIMEOUT_S):
            raise ValueError("rate, concurrency, duration_s, and timeout_s must be finite, positive, and within limits")
        if self.queue_size < 0 or self.queue_size > MAX_QUEUE_SIZE:
            raise ValueError("queue_size must be within limits")
        if not math.isfinite(self.employee_share) or not 0 <= self.employee_share <= 1:
            raise ValueError("employee_share must be finite and between 0 and 1")
        if not isinstance(self.max_response_bytes, int) or not 1 <= self.max_response_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError("max_response_bytes must be within limits")
        if not self.employee_session_cookie or not self.machine_bearer_token:
            raise ValueError("employee session cookie and machine bearer token are required")


@dataclass(frozen=True)
class RequestSpec:
    kind: str
    variant: str
    path: str
    payload: dict


@dataclass(frozen=True)
class Sample:
    kind: str
    variant: str
    status: int
    elapsed_ms: float
    error: str | None = None
    queue_wait_ms: float = 0.0
    request_ms: float = 0.0
    cache_hit: bool | None = None


def percentile(values, p):
    if not 0 <= p <= 1:
        raise ValueError("p must be between 0 and 1")
    if not values:
        return 0.0
    return sorted(values)[min(len(values) - 1, max(0, math.ceil(len(values) * p) - 1))]


def _field(sample, name):
    return sample.get(name) if isinstance(sample, dict) else getattr(sample, name)


def _times(samples, field):
    return [_field(sample, field) for sample in samples
            if isinstance(_field(sample, field), (int, float))
            and math.isfinite(_field(sample, field)) and _field(sample, field) >= 0]


def _summary(samples, dropped=0, duration_s=0.0):
    count = len(samples)
    attempted = count + dropped
    successes = sum(200 <= _field(sample, "status") < 300 for sample in samples)
    errors = attempted - successes
    throttled = sum(_field(sample, "status") == 429 for sample in samples)
    latencies = _times(samples, "elapsed_ms")
    request_times = _times(samples, "request_ms")
    queue_waits = _times(samples, "queue_wait_ms")
    completed_rps = count / duration_s if duration_s > 0 else 0.0
    successful_rps = successes / duration_s if duration_s > 0 else 0.0
    cache_values = [_field(sample, "cache_hit") for sample in samples
                    if _field(sample, "cache_hit") is not None]
    return {
        "requests": attempted,
        "completed": count,
        "dropped": dropped,
        "success_rate": successes / attempted if attempted else 0.0,
        "error_rate": errors / attempted if attempted else 0.0,
        "429_count": throttled,
        "429_rate": throttled / attempted if attempted else 0.0,
        "p50_ms": percentile(latencies, 0.50),
        "p95_ms": percentile(latencies, 0.95),
        "p99_ms": percentile(latencies, 0.99),
        "request_p50_ms": percentile(request_times, 0.50),
        "request_p95_ms": percentile(request_times, 0.95),
        "request_p99_ms": percentile(request_times, 0.99),
        "queue_wait_p95_ms": percentile(queue_waits, 0.95),
        "throughput_rps": completed_rps,
        "completed_rps": completed_rps,
        "successful_rps": successful_rps,
        "cache_hit_count": sum(cache_values),
        "cache_observation_count": len(cache_values),
        "cache_hit_rate": (sum(cache_values) / len(cache_values) if cache_values else None),
    }


def aggregate(samples, dropped=0, duration_s=0.0, dropped_by_kind=None):
    """Aggregate only supplied measurements; this function never performs I/O."""
    samples = list(samples)
    dropped_by_kind = dropped_by_kind or {}
    result = _summary(samples, dropped, duration_s)
    result["status_counts"] = {}
    result["dropped_by_kind"] = {
        kind: int(dropped_by_kind.get(kind, 0)) for kind in ("employee", "machine")
    }
    for sample in samples:
        status = str(_field(sample, "status"))
        result["status_counts"][status] = result["status_counts"].get(status, 0) + 1
    for kind in ("employee", "machine"):
        group = [sample for sample in samples if _field(sample, "kind") == kind]
        result[kind] = _summary(group, result["dropped_by_kind"][kind], duration_s)
    for variant in ("warm", "varied"):
        group = [sample for sample in samples if _field(sample, "variant") == variant]
        result[variant] = _summary(group, 0, duration_s)
    return result


def request_spec(index, employee_share):
    employee = math.floor((index + 1) * employee_share) > math.floor(index * employee_share)
    variant = "warm" if index % 2 == 0 else "varied"
    query = WARM_QUERY if variant == "warm" else VARIED_QUERIES[(index // 2) % len(VARIED_QUERIES)]
    if employee:
        return RequestSpec("employee", variant, "/api/v1/rag/answer",
                           {"query": query, "limit": 3, "temperature": 0})
    return RequestSpec("machine", variant, "/api/v1/retrieval/search",
                       {"query": query, "limit": 8})


def _open_no_redirect(request, timeout):
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


def _cache_hit(response, body):
    headers = getattr(response, "headers", {})
    for name in ("X-Cache-Hit", "X-Cache", "X-Response-Cache"):
        value = headers.get(name) if hasattr(headers, "get") else None
        if value is not None:
            normalized = str(value).strip().lower()
            if normalized in {"hit", "true", "1", "yes"}:
                return True
            if normalized in {"miss", "false", "0", "no"}:
                return False
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, TypeError, ValueError):
        return None
    if isinstance(payload, dict):
        for key in ("cache_hit", "cacheHit"):
            if isinstance(payload.get(key), bool):
                return payload[key]
        cache = payload.get("cache")
        if isinstance(cache, dict) and isinstance(cache.get("hit"), bool):
            return cache["hit"]
    return None


def request_one(config, spec, opener=None, scheduled_at=None):
    headers = {"Content-Type": "application/json"}
    if spec.kind == "employee":
        headers["Cookie"] = config.employee_session_cookie
    else:
        headers["Authorization"] = f"Bearer {config.machine_bearer_token}"
    request = Request(
        config.base_url.rstrip("/") + spec.path,
        data=json.dumps(spec.payload, ensure_ascii=False).encode(),
        headers=headers,
    )
    scheduled_at = time.monotonic() if scheduled_at is None else scheduled_at
    request_started = time.monotonic()
    queue_wait_ms = max(0.0, request_started - scheduled_at) * 1000
    try:
        open_request = opener or _open_no_redirect
        with open_request(request, timeout=config.timeout_s) as response:
            body = response.read(config.max_response_bytes + 1)
            if len(body) > config.max_response_bytes:
                raise ValueError("response exceeds configured limit")
            finished = time.monotonic()
            request_ms = (finished - request_started) * 1000
            return Sample(spec.kind, spec.variant, response.status,
                          (finished - scheduled_at) * 1000,
                          queue_wait_ms=queue_wait_ms, request_ms=request_ms,
                          cache_hit=_cache_hit(response, body))
    except HTTPError as exc:
        finished = time.monotonic()
        request_ms = (finished - request_started) * 1000
        return Sample(spec.kind, spec.variant, exc.code, (finished - scheduled_at) * 1000,
                      f"HTTP {exc.code}", queue_wait_ms, request_ms)
    except Exception as exc:
        finished = time.monotonic()
        request_ms = (finished - request_started) * 1000
        return Sample(spec.kind, spec.variant, 0, (finished - scheduled_at) * 1000,
                      type(exc).__name__, queue_wait_ms, request_ms)


def run_load(config, opener=None):
    config.validate()
    started = time.monotonic()
    deadline = started + config.duration_s
    interval = 1 / config.rate
    next_dispatch = started
    sequence = 0
    dropped = 0
    dropped_by_kind = {"employee": 0, "machine": 0}
    samples = []
    pending = {}
    max_outstanding = config.concurrency + config.queue_size

    with ThreadPoolExecutor(max_workers=config.concurrency) as pool:
        while pending or time.monotonic() < deadline:
            now = time.monotonic()
            while now >= next_dispatch and next_dispatch < deadline:
                spec = request_spec(sequence, config.employee_share)
                if len(pending) >= max_outstanding:
                    dropped += 1
                    dropped_by_kind[spec.kind] += 1
                else:
                    pending[pool.submit(request_one, config, spec, opener=opener,
                                        scheduled_at=next_dispatch)] = spec
                sequence += 1
                next_dispatch += interval
                now = time.monotonic()
            if pending:
                done, _ = wait(tuple(pending), timeout=0.05, return_when=FIRST_COMPLETED)
                for future in done:
                    samples.append(future.result())
                    pending.pop(future, None)
            elif time.monotonic() < deadline:
                time.sleep(min(0.05, max(0, next_dispatch - time.monotonic())))

    elapsed = time.monotonic() - started
    result = aggregate(samples, dropped, elapsed, dropped_by_kind)
    result["config"] = {
        "rate": config.rate,
        "concurrency": config.concurrency,
        "duration_s": config.duration_s,
        "timeout_s": config.timeout_s,
        "queue_size": config.queue_size,
        "employee_share": config.employee_share,
        "max_response_bytes": config.max_response_bytes,
        "employee_auth": "session_cookie",
        "machine_auth": "bearer",
        "varied_query_set": len(VARIED_QUERIES),
    }
    return result


def main():
    parser = argparse.ArgumentParser(description="Run an opt-in, read-only RAG capacity probe.")
    parser.add_argument("--rate", type=float, default=1.0, help="target requests per second")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--duration", type=float, default=30.0, help="duration in seconds")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-request timeout in seconds")
    parser.add_argument("--queue-size", type=int, default=8, help="bounded queue beyond active workers")
    parser.add_argument("--employee-share", type=float, default=0.8)
    args = parser.parse_args()
    config = LoadConfig.from_env(rate=args.rate, concurrency=args.concurrency, duration_s=args.duration,
                                 timeout_s=args.timeout, queue_size=args.queue_size,
                                 employee_share=args.employee_share)
    print(json.dumps(run_load(config), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
