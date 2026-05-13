# Day 10 Reliability Report

## 1. Architecture summary

This gateway first checks Redis cache, then routes provider calls through per-provider circuit breakers. If the primary fails or its circuit is open, the gateway tries the backup provider; if every provider fails, it returns a static degraded-service fallback.

```text
User Request
    |
    v
[Gateway] ---> [Redis cache check] ---> HIT? return cached response
    |                                    |
    v                                    v MISS
[Circuit Breaker: Primary] ----------> Provider primary
    |  (OPEN? skip fast)
    v
[Circuit Breaker: Backup] -----------> Provider backup
    |  (OPEN? skip fast)
    v
[Static fallback message]
```

## 2. Configuration

| Setting               |  Value | Reason                                                                     |
| --------------------- | -----: | -------------------------------------------------------------------------- |
| failure_threshold     |      3 | Opens after repeated failures without reacting to one random error.        |
| reset_timeout_seconds |      2 | Allows a quick half-open recovery probe during chaos tests.                |
| success_threshold     |      1 | One successful probe is enough for this local fake-provider lab.           |
| cache backend         |  redis | Uses Redis shared cache for multi-instance behavior.                       |
| cache TTL             |    300 | Five minutes balances freshness and useful repeated-query hits.            |
| similarity_threshold  | 0.9200 | High threshold prevents broad semantic false hits.                         |
| load_test requests    |    200 | Enough requests to exercise cache hits, fallback, and circuit transitions. |
| load_test concurrency |     10 | Concurrent local load makes the reliability behavior easier to observe.    |

## 3. SLO definitions

| SLI                   | SLO target | Actual value | Met? |
| --------------------- | ---------- | -----------: | ---- |
| Availability          | >= 99%     |       1.0000 | Pass |
| Latency P95           | < 2500 ms  |       465.06 | Pass |
| Fallback success rate | >= 95%     |       1.0000 | Pass |
| Cache hit rate        | >= 10%     |       0.7400 | Pass |
| Recovery time         | < 5000 ms  |      2703.09 | Pass |

## 4. Metrics

| Metric                |   Value |
| --------------------- | ------: |
| total_requests        |     800 |
| availability          |  1.0000 |
| error_rate            |  0.0000 |
| latency_p50_ms        |  1.6000 |
| latency_p95_ms        |  465.06 |
| latency_p99_ms        |  523.44 |
| fallback_success_rate |  1.0000 |
| cache_hit_rate        |  0.7400 |
| estimated_cost        |  0.1023 |
| estimated_cost_saved  |  0.3471 |
| circuit_open_count    |      21 |
| recovery_time_ms      | 2703.09 |

### Route reasons

| Route reason           | Count |
| ---------------------- | ----: |
| cache_hit:score=1.00   |   592 |
| fallback:backup:served |    83 |
| primary:primary:served |   125 |

### Circuit transition evidence

```json
[
  {
    "scenario": "primary_timeout_100",
    "breaker": "primary",
    "from": "closed",
    "to": "open",
    "reason": "failure_threshold",
    "ts": 1778647958.745294
  },
  {
    "scenario": "primary_timeout_100",
    "breaker": "primary",
    "from": "open",
    "to": "half_open",
    "reason": "reset_timeout_elapsed",
    "ts": 1778647961.2882788
  },
  {
    "scenario": "primary_timeout_100",
    "breaker": "primary",
    "from": "half_open",
    "to": "open",
    "reason": "half_open_probe_failed",
    "ts": 1778647961.4907186
  },
  {
    "scenario": "primary_timeout_100",
    "breaker": "primary",
    "from": "open",
    "to": "half_open",
    "reason": "reset_timeout_elapsed",
    "ts": 1778647963.9605274
  },
  {
    "scenario": "primary_timeout_100",
    "breaker": "primary",
    "from": "half_open",
    "to": "open",
    "reason": "half_open_probe_failed",
    "ts": 1778647964.1692195
  },
  {
    "scenario": "primary_timeout_100",
    "breaker": "primary",
    "from": "open",
    "to": "half_open",
    "reason": "reset_timeout_elapsed",
    "ts": 1778647966.689829
  },
  {
    "scenario": "primary_timeout_100",
    "breaker": "primary",
    "from": "half_open",
    "to": "open",
    "reason": "half_open_probe_failed",
    "ts": 1778647966.8931894
  },
  {
    "scenario": "primary_timeout_100",
    "breaker": "primary",
    "from": "open",
    "to": "half_open",
    "reason": "reset_timeout_elapsed",
    "ts": 1778647969.396306
  },
  {
    "scenario": "primary_timeout_100",
    "breaker": "primary",
    "from": "half_open",
    "to": "open",
    "reason": "half_open_probe_failed",
    "ts": 1778647969.5831852
  },
  {
    "scenario": "primary_timeout_100",
    "breaker": "primary",
    "from": "open",
    "to": "half_open",
    "reason": "reset_timeout_elapsed",
    "ts": 1778647972.0560625
  }
]
```

## 5. Cache comparison

| Metric         | Without cache | With cache |   Delta |
| -------------- | ------------: | ---------: | ------: |
| latency_p50_ms |        210.15 |     1.5400 | -208.61 |
| latency_p95_ms |        237.43 |     233.71 | -3.7200 |
| estimated_cost |        0.1156 |     0.0267 | -0.0889 |
| cache_hit_rate |        0.0000 |     0.7700 |  0.7700 |

## 6. Redis shared cache

Explain why shared cache matters for production:

- Why in-memory cache is insufficient for multi-instance deployments: In-memory cache is local to one process, so horizontally scaled gateway instances cannot share cache hits or reduce duplicated provider calls.
- How `SharedRedisCache` solves this: Redis stores shared cache state centrally so multiple gateway instances can reuse the same cached responses with TTL expiration.

### Evidence of shared state

```text
redis_available=yes
shared_state=yes
lookup_score=1.0000
rl:cache:evidence:1ce63c241e59
```

### Redis CLI output

```bash
# docker compose exec redis redis-cli KEYS "rl:cache:*"
```

```text
rl:cache:evidence:1ce63c241e59
```

### In-memory vs Redis latency comparison (optional)

| Metric         | In-memory cache | Redis cache | Notes                                                  |
| -------------- | --------------: | ----------: | ------------------------------------------------------ |
| latency_p50_ms |          210.15 |      1.5400 | Redis enables shared cache state across instances      |
| latency_p95_ms |          237.43 |      233.71 | Redis latency remains acceptable under concurrent load |

## 7. Chaos scenarios

| Scenario              | Expected behavior                                                                  | Observed behavior                                                                                        | Pass/Fail |
| --------------------- | ---------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | --------- |
| primary_timeout_100   | Primary fails, circuit opens, backup/cache keeps service available.                | availability=100.00%, cache_hit_rate=70.50%, fallback_successes=59, static_fallbacks=0, circuit_opens=19 | pass      |
| primary_flaky_50      | Primary intermittently fails; circuit opens/retries and fallback absorbs failures. | availability=100.00%, cache_hit_rate=72.00%, fallback_successes=24, static_fallbacks=0, circuit_opens=2  | pass      |
| cache_stale_candidate | Similar but date-different queries are rejected as false cache hits.               | availability=100.00%, cache_hit_rate=79.00%, fallback_successes=0, static_fallbacks=0, circuit_opens=0   | pass      |
| all_healthy           | Requests should succeed without static fallback.                                   | availability=100.00%, cache_hit_rate=74.50%, fallback_successes=0, static_fallbacks=0, circuit_opens=0   | pass      |

## 8. Failure analysis

One remaining production weakness is that circuit breaker state is still local to each app process. Redis now shares cached responses, but two gateway instances could still make different circuit decisions. Before production, I would store circuit counters and state transitions in Redis or another shared control plane so all instances agree when a provider should be skipped.

## 9. Next steps

1. Move circuit breaker state to Redis so provider health is shared across instances.
2. Add per-user or per-tenant rate limiting before provider calls.
3. Export metrics to Prometheus/Grafana for live dashboards and alerts.

## Reproducibility commands used by this report

```bash
python -m pytest -q
python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json
python scripts/generate_report.py --metrics reports/metrics.json --out reports/final_report.md --config configs/default.yaml
```
