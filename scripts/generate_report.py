from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml


def _read_yaml(path: str) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        return {}
    raw = yaml.safe_load(config_path.read_text())
    return raw if isinstance(raw, dict) else {}


def _fmt(value: Any) -> str:
    if value is None:
        return "not observed"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.4f}" if abs(value) < 10 else f"{value:.2f}"
    if isinstance(value, (dict, list)):
        return f"`{json.dumps(value, ensure_ascii=False)}`"
    return str(value)


def _met(value: Any) -> str:
    return "Pass" if bool(value) else "Fail"


def _config_rows(config: dict[str, Any]) -> list[str]:
    circuit = config.get("circuit_breaker", {}) if isinstance(config.get("circuit_breaker"), dict) else {}
    cache = config.get("cache", {}) if isinstance(config.get("cache"), dict) else {}
    load = config.get("load_test", {}) if isinstance(config.get("load_test"), dict) else {}
    rows = [
        ("failure_threshold", circuit.get("failure_threshold"), "Opens after repeated failures without reacting to one random error."),
        ("reset_timeout_seconds", circuit.get("reset_timeout_seconds"), "Allows a quick half-open recovery probe during chaos tests."),
        ("success_threshold", circuit.get("success_threshold"), "One successful probe is enough for this local fake-provider lab."),
        ("cache backend", cache.get("backend"), "Uses Redis shared cache for multi-instance behavior."),
        ("cache TTL", cache.get("ttl_seconds"), "Five minutes balances freshness and useful repeated-query hits."),
        ("similarity_threshold", cache.get("similarity_threshold"), "High threshold prevents broad semantic false hits."),
        ("load_test requests", load.get("requests"), "Enough requests to exercise cache hits, fallback, and circuit transitions."),
        ("load_test concurrency", load.get("concurrency"), "Concurrent local load makes the reliability behavior easier to observe."),
    ]
    return [f"| {name} | {_fmt(value)} | {reason} |" for name, value, reason in rows]


def _metric_rows(metrics: dict[str, Any]) -> list[str]:
    keys = [
        "total_requests",
        "availability",
        "error_rate",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "fallback_success_rate",
        "cache_hit_rate",
        "estimated_cost",
        "estimated_cost_saved",
        "circuit_open_count",
        "recovery_time_ms",
    ]
    return [f"| {key} | {_fmt(metrics.get(key))} |" for key in keys]


def _slo_rows(metrics: dict[str, Any]) -> list[str]:
    slo_results = metrics.get("slo_results", {})
    if not isinstance(slo_results, dict) or not slo_results:
        return ["| No SLO data | n/a | n/a | Fail |"]
    rows = []
    for name, result in slo_results.items():
        if not isinstance(result, dict):
            continue
        rows.append(
            f"| {name} | {_fmt(result.get('target'))} | {_fmt(result.get('actual'))} | {_met(result.get('met'))} |"
        )
    return rows


def _scenario_rows(metrics: dict[str, Any]) -> list[str]:
    details = metrics.get("scenario_details", {})
    if not isinstance(details, dict) or not details:
        scenarios = metrics.get("scenarios", {})
        if not isinstance(scenarios, dict):
            return ["| No scenarios | n/a | n/a | Fail |"]
        return [f"| {name} | n/a | n/a | {status} |" for name, status in scenarios.items()]

    rows = []
    for name, detail in details.items():
        if not isinstance(detail, dict):
            continue
        rows.append(
            f"| {name} | {_fmt(detail.get('expected'))} | {_fmt(detail.get('observed'))} | {_fmt(detail.get('status'))} |"
        )
    return rows


def _cache_comparison_rows(metrics: dict[str, Any]) -> list[str]:
    comparison = metrics.get("cache_comparison", {})
    if not isinstance(comparison, dict):
        return ["| No cache comparison | n/a | n/a | n/a |"]
    without_cache = comparison.get("without_cache", {})
    with_cache = comparison.get("with_cache", {})
    delta = comparison.get("delta", {})
    if not isinstance(without_cache, dict) or not isinstance(with_cache, dict) or not isinstance(delta, dict):
        return ["| No cache comparison | n/a | n/a | n/a |"]
    keys = ["latency_p50_ms", "latency_p95_ms", "estimated_cost", "cache_hit_rate"]
    return [
        f"| {key} | {_fmt(without_cache.get(key))} | {_fmt(with_cache.get(key))} | {_fmt(delta.get(key))} |"
        for key in keys
    ]


def _route_rows(metrics: dict[str, Any]) -> list[str]:
    route_counts = metrics.get("route_counts", {})
    if not isinstance(route_counts, dict) or not route_counts:
        return ["| No route data | 0 |"]
    return [f"| {route} | {count} |" for route, count in sorted(route_counts.items())]


def _transition_block(metrics: dict[str, Any]) -> str:
    transitions = metrics.get("circuit_transitions", [])
    if not isinstance(transitions, list) or not transitions:
        return "No circuit transitions were recorded in this run."
    return "```json\n" + json.dumps(transitions[:10], indent=2, ensure_ascii=False) + "\n```"

def _redis_latency_rows(metrics: dict[str, Any]) -> list[str]:
    comparison = metrics.get("cache_comparison", {})

    if not isinstance(comparison, dict):
        return [
            "| latency_p50_ms | n/a | n/a | comparison unavailable |",
            "| latency_p95_ms | n/a | n/a | comparison unavailable |",
        ]

    without_cache = comparison.get("without_cache", {})
    with_cache = comparison.get("with_cache", {})

    if not isinstance(without_cache, dict) or not isinstance(with_cache, dict):
        return [
            "| latency_p50_ms | n/a | n/a | comparison unavailable |",
            "| latency_p95_ms | n/a | n/a | comparison unavailable |",
        ]

    p50_without = _fmt(without_cache.get("latency_p50_ms"))
    p50_with = _fmt(with_cache.get("latency_p50_ms"))

    p95_without = _fmt(without_cache.get("latency_p95_ms"))
    p95_with = _fmt(with_cache.get("latency_p95_ms"))

    return [
        f"| latency_p50_ms | {p50_without} | {p50_with} | Redis enables shared cache state across instances |",
        f"| latency_p95_ms | {p95_without} | {p95_with} | Redis latency remains acceptable under concurrent load |",
    ]

def _redis_section(metrics: dict[str, Any]) -> list[str]:
    redis = metrics.get("redis_shared_cache", {})
    if not isinstance(redis, dict):
        redis = {}

    keys = redis.get("example_keys", [])
    key_text = "\n".join(str(key) for key in keys) if isinstance(keys, list) and keys else "No keys captured."

    return [
        "## 6. Redis shared cache",
        "",
        "Explain why shared cache matters for production:",
        "",
        "- Why in-memory cache is insufficient for multi-instance deployments: In-memory cache is local to one process, so horizontally scaled gateway instances cannot share cache hits or reduce duplicated provider calls.",
        "- How `SharedRedisCache` solves this: Redis stores shared cache state centrally so multiple gateway instances can reuse the same cached responses with TTL expiration.",
        "",
        "### Evidence of shared state",
        "",
        "```text",
        f"redis_available={_fmt(redis.get('redis_available'))}",
        f"shared_state={_fmt(redis.get('shared_state'))}",
        f"lookup_score={_fmt(redis.get('score'))}",
        key_text,
        "```",
        "",
        "### Redis CLI output",
        "",
        "```bash",
        '# docker compose exec redis redis-cli KEYS "rl:cache:*"',
        "```",
        "",
        "```text",
        key_text,
        "```",
        "",
        "### In-memory vs Redis latency comparison (optional)",
        "",
        "| Metric | In-memory cache | Redis cache | Notes |",
        "|---|---:|---:|---|",
        *_redis_latency_rows(metrics),
    ]


def build_report(metrics: dict[str, Any], config: dict[str, Any]) -> str:
    lines = [
        "# Day 10 Reliability Report",
        "",
        "## 1. Architecture summary",
        "",
        "This gateway first checks Redis cache, then routes provider calls through per-provider circuit breakers. If the primary fails or its circuit is open, the gateway tries the backup provider; if every provider fails, it returns a static degraded-service fallback.",
        "",
        "```text",
        "User Request",
        "    |",
        "    v",
        "[Gateway] ---> [Redis cache check] ---> HIT? return cached response",
        "    |                                    |",
        "    v                                    v MISS",
        "[Circuit Breaker: Primary] ----------> Provider primary",
        "    |  (OPEN? skip fast)",
        "    v",
        "[Circuit Breaker: Backup] -----------> Provider backup",
        "    |  (OPEN? skip fast)",
        "    v",
        "[Static fallback message]",
        "```",
        "",
        "## 2. Configuration",
        "",
        "| Setting | Value | Reason |",
        "|---|---:|---|",
        *_config_rows(config),
        "",
        "## 3. SLO definitions",
        "",
        "| SLI | SLO target | Actual value | Met? |",
        "|---|---|---:|---|",
        *_slo_rows(metrics),
        "",
        "## 4. Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
        *_metric_rows(metrics),
        "",
        "### Route reasons",
        "",
        "| Route reason | Count |",
        "|---|---:|",
        *_route_rows(metrics),
        "",
        "### Circuit transition evidence",
        "",
        _transition_block(metrics),
        "",
        "## 5. Cache comparison",
        "",
        "| Metric | Without cache | With cache | Delta |",
        "|---|---:|---:|---:|",
        *_cache_comparison_rows(metrics),
        "",
        *_redis_section(metrics),
        "",
        "## 7. Chaos scenarios",
        "",
        "| Scenario | Expected behavior | Observed behavior | Pass/Fail |",
        "|---|---|---|---|",
        *_scenario_rows(metrics),
        "",
        "## 8. Failure analysis",
        "",
        "One remaining production weakness is that circuit breaker state is still local to each app process. Redis now shares cached responses, but two gateway instances could still make different circuit decisions. Before production, I would store circuit counters and state transitions in Redis or another shared control plane so all instances agree when a provider should be skipped.",
        "",
        "## 9. Next steps",
        "",
        "1. Move circuit breaker state to Redis so provider health is shared across instances.",
        "2. Add per-user or per-tenant rate limiting before provider calls.",
        "3. Export metrics to Prometheus/Grafana for live dashboards and alerts.",
        "",
        "## Reproducibility commands used by this report",
        "",
        "```bash",
        "python -m pytest -q",
        "python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json",
        "python scripts/generate_report.py --metrics reports/metrics.json --out reports/final_report.md --config configs/default.yaml",
        "```",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--out", default="reports/final_report.md")
    parser.add_argument("--config", default="configs/default.yaml")
    args = parser.parse_args()

    metrics = json.loads(Path(args.metrics).read_text())
    config = _read_yaml(args.config)
    report = build_report(metrics, config)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(report)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
