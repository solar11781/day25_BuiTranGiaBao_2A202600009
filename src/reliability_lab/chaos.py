from __future__ import annotations

import json
import time
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import GatewayResponse, ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(str(json.loads(line)["query"]))
    return queries


def build_gateway(config: LabConfig, provider_overrides: dict[str, float] | None = None) -> ReliabilityGateway:
    providers: list[FakeLLMProvider] = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))

    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }

    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        elif config.cache.backend == "memory":
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
        else:
            raise ValueError(f"unsupported cache backend: {config.cache.backend}")

    return ReliabilityGateway(providers, breakers, cache)


def _config_for_scenario(config: LabConfig, scenario: ScenarioConfig) -> LabConfig:
    scenario_config = config.model_copy(deep=True)
    if scenario.cache_enabled is not None:
        scenario_config.cache.enabled = scenario.cache_enabled
    if scenario.cache_backend is not None:
        scenario_config.cache.backend = scenario.cache_backend
    if scenario.cache_similarity_threshold is not None:
        scenario_config.cache.similarity_threshold = scenario.cache_similarity_threshold
    return scenario_config


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive average recovery time from circuit breaker transition logs."""
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        open_ts: float | None = None
        for entry in breaker.transition_log:
            if entry["to"] == "open" and open_ts is None:
                open_ts = float(entry["ts"])
            elif entry["to"] == "closed" and open_ts is not None:
                recovery_times.append((float(entry["ts"]) - open_ts) * 1000)
                open_ts = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def _estimated_cache_savings(prompt: str, config: LabConfig) -> float:
    if not config.providers:
        return 0.0
    provider = config.providers[0]
    estimated_tokens = max(1, len(prompt.split())) + 50
    return estimated_tokens / 1000.0 * provider.cost_per_1k_tokens


def _record_result(metrics: RunMetrics, result: GatewayResponse, prompt: str, config: LabConfig) -> None:
    metrics.total_requests += 1
    metrics.estimated_cost += result.estimated_cost
    metrics.add_route(result.route_reason or result.route)

    if result.cache_hit:
        metrics.cache_hits += 1
        metrics.estimated_cost_saved += _estimated_cache_savings(prompt, config)
        metrics.successful_requests += 1
    elif result.route == "fallback":
        metrics.fallback_successes += 1
        metrics.successful_requests += 1
    elif result.route == "static_fallback":
        metrics.static_fallbacks += 1
        metrics.failed_requests += 1
    else:
        metrics.successful_requests += 1

    metrics.latencies_ms.append(result.latency_ms)


def _run_prompts(gateway: ReliabilityGateway, prompts: list[str], concurrency: int) -> list[GatewayResponse]:
    results: list[GatewayResponse] = []

    if concurrency <= 1:
        for index, prompt in enumerate(prompts):
            result = gateway.complete(prompt)
            results.append(result)

            # IMPORTANT:
            # Allow OPEN circuits enough time to transition into HALF_OPEN.
            # Without this pause, recovery transitions never occur and
            # recovery_time_ms remains null.
            if index > 0 and index % 25 == 0:
                time.sleep(2.2)

        return results

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        batch_size = max(concurrency, 10)

        for start in range(0, len(prompts), batch_size):
            batch = prompts[start : start + batch_size]
            batch_results = list(executor.map(gateway.complete, batch))
            results.extend(batch_results)

            # IMPORTANT:
            # Pause between batches so reset_timeout_seconds can expire.
            # This enables:
            # OPEN -> HALF_OPEN -> CLOSED
            # transitions during chaos testing.
            time.sleep(2.2)

    return results


def _cache_false_hit_evidence(cache: ResponseCache | SharedRedisCache | None) -> dict[str, object]:
    if cache is None:
        return {"checked": False, "reason": "cache disabled"}

    cache.set("Summarize refund policy for 2024 deadline", "old refund policy")
    cached, score = cache.get("Summarize refund policy for 2026 deadline")
    false_hit_log = getattr(cache, "false_hit_log", [])
    return {
        "checked": True,
        "false_hit_prevented": cached is None,
        "similarity_score": round(score, 4),
        "false_hit_log_entries": len(false_hit_log),
        "example": "2024 vs 2026 refund-policy queries must not share a cached answer",
    }


def _transition_entries(gateway: ReliabilityGateway, scenario_name: str) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for breaker_name, breaker in gateway.breakers.items():
        for transition in breaker.transition_log:
            entries.append({"scenario": scenario_name, "breaker": breaker_name, **transition})
    return entries


def _scenario_passed(name: str, metrics: RunMetrics, details: dict[str, object]) -> bool:
    if name == "primary_timeout_100":
        return metrics.availability >= 0.95 and metrics.static_fallbacks == 0 and (
            metrics.fallback_successes > 0 or metrics.cache_hits > 0
        )
    if name == "primary_flaky_50":
        return metrics.availability >= 0.85 and (
            metrics.fallback_successes > 0 or metrics.circuit_open_count > 0
        )
    if name == "cache_stale_candidate":
        return bool(details.get("false_hit_prevented")) and metrics.availability >= 0.95
    if name == "all_healthy":
        return metrics.availability >= 0.99 and metrics.static_fallbacks == 0
    return metrics.successful_requests > 0


def _observed_summary(metrics: RunMetrics) -> str:
    return (
        f"availability={metrics.availability:.2%}, cache_hit_rate={metrics.cache_hit_rate:.2%}, "
        f"fallback_successes={metrics.fallback_successes}, static_fallbacks={metrics.static_fallbacks}, "
        f"circuit_opens={metrics.circuit_open_count}"
    )


def run_scenario(
    config: LabConfig,
    queries: list[str],
    scenario: ScenarioConfig,
    prompt_sequence: list[str] | None = None,
) -> RunMetrics:
    """Run a single named chaos scenario."""
    scenario_config = _config_for_scenario(config, scenario)
    gateway = build_gateway(scenario_config, scenario.provider_overrides or None)

    if gateway.cache is not None and hasattr(gateway.cache, "flush"):
        gateway.cache.flush()

    rng = random.Random(scenario.name)
    prompts = prompt_sequence or [rng.choice(queries) for _ in range(scenario_config.load_test.requests)]
    results = _run_prompts(gateway, prompts, scenario_config.load_test.concurrency)

    metrics = RunMetrics()
    for prompt, result in zip(prompts, results):
        _record_result(metrics, result, prompt, scenario_config)

    metrics.circuit_open_count = sum(
        1 for breaker in gateway.breakers.values() for item in breaker.transition_log if item["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    metrics.circuit_transitions = _transition_entries(gateway, scenario.name)

    details: dict[str, object] = {
        "description": scenario.description,
        "expected": _expected_behavior(scenario.name),
        "observed": _observed_summary(metrics),
        "requests": metrics.total_requests,
        "concurrency": scenario_config.load_test.concurrency,
    }

    if scenario.name == "cache_stale_candidate":
        details.update(_cache_false_hit_evidence(gateway.cache))

    passed = _scenario_passed(scenario.name, metrics, details)
    metrics.scenarios = {scenario.name: "pass" if passed else "fail"}
    metrics.scenario_details = {scenario.name: details | {"status": metrics.scenarios[scenario.name]}}
    return metrics


def _expected_behavior(name: str) -> str:
    expected = {
        "primary_timeout_100": "Primary fails, circuit opens, backup/cache keeps service available.",
        "primary_flaky_50": "Primary intermittently fails; circuit opens/retries and fallback absorbs failures.",
        "cache_stale_candidate": "Similar but date-different queries are rejected as false cache hits.",
        "all_healthy": "Requests should succeed without static fallback.",
    }
    return expected.get(name, "Scenario should preserve availability and emit metrics.")


def _merge_metrics(combined: RunMetrics, result: RunMetrics) -> None:
    combined.total_requests += result.total_requests
    combined.successful_requests += result.successful_requests
    combined.failed_requests += result.failed_requests
    combined.fallback_successes += result.fallback_successes
    combined.static_fallbacks += result.static_fallbacks
    combined.cache_hits += result.cache_hits
    combined.circuit_open_count += result.circuit_open_count
    combined.estimated_cost += result.estimated_cost
    combined.estimated_cost_saved += result.estimated_cost_saved
    combined.latencies_ms.extend(result.latencies_ms)
    combined.circuit_transitions.extend(result.circuit_transitions)
    combined.scenarios.update(result.scenarios)
    combined.scenario_details.update(result.scenario_details)
    for route, count in result.route_counts.items():
        combined.route_counts[route] = combined.route_counts.get(route, 0) + count
    if result.recovery_time_ms is not None:
        if combined.recovery_time_ms is None:
            combined.recovery_time_ms = result.recovery_time_ms
        else:
            combined.recovery_time_ms = (combined.recovery_time_ms + result.recovery_time_ms) / 2


def _metrics_snapshot(metrics: RunMetrics) -> dict[str, object]:
    return {
        "latency_p50_ms": round(metrics.percentile(50), 2),
        "latency_p95_ms": round(metrics.percentile(95), 2),
        "estimated_cost": round(metrics.estimated_cost, 6),
        "cache_hit_rate": round(metrics.cache_hit_rate, 4),
    }


def _delta(before: object, after: object) -> object:
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return round(after - before, 6)
    return "n/a"


def run_cache_comparison(config: LabConfig, queries: list[str]) -> dict[str, dict[str, object]]:
    """Run cache disabled vs enabled with healthy providers for report evidence."""
    rng = random.Random("cache_comparison")
    prompts = [rng.choice(queries) for _ in range(config.load_test.requests)]
    healthy_overrides = {provider.name: 0.0 for provider in config.providers}

    without_cache = run_scenario(
        config,
        queries,
        ScenarioConfig(
            name="cache_comparison_without_cache",
            description="Healthy providers, cache disabled.",
            provider_overrides=healthy_overrides,
            cache_enabled=False,
        ),
        prompt_sequence=prompts,
    )
    with_cache = run_scenario(
        config,
        queries,
        ScenarioConfig(
            name="cache_comparison_with_cache",
            description="Healthy providers, cache enabled.",
            provider_overrides=healthy_overrides,
            cache_enabled=True,
        ),
        prompt_sequence=prompts,
    )

    without_snapshot = _metrics_snapshot(without_cache)
    with_snapshot = _metrics_snapshot(with_cache)
    delta = {key: _delta(without_snapshot[key], with_snapshot[key]) for key in without_snapshot}
    return {"without_cache": without_snapshot, "with_cache": with_snapshot, "delta": delta}


def verify_redis_shared_cache(config: LabConfig) -> dict[str, object]:
    """Demonstrate that two Redis cache instances share state."""
    if config.cache.backend != "redis":
        return {"backend": config.cache.backend, "checked": False, "reason": "backend is not redis"}

    c1 = SharedRedisCache(
        config.cache.redis_url,
        config.cache.ttl_seconds,
        config.cache.similarity_threshold,
        prefix="rl:cache:evidence:",
    )
    c2 = SharedRedisCache(
        config.cache.redis_url,
        config.cache.ttl_seconds,
        config.cache.similarity_threshold,
        prefix="rl:cache:evidence:",
    )
    try:
        if not c1.ping() or not c2.ping():
            return {"backend": "redis", "checked": True, "redis_available": False, "shared_state": False}
        c1.flush()
        c1.set("shared redis evidence query", "shared redis evidence response")
        cached, score = c2.get("shared redis evidence query")
        keys = c1.keys()
        return {
            "backend": "redis",
            "checked": True,
            "redis_available": True,
            "shared_state": cached == "shared redis evidence response",
            "score": score,
            "example_key_count": len(keys),
            "example_keys": keys[:10],
            "cli_command": 'docker compose exec redis redis-cli KEYS "rl:cache:*"',
        }
    finally:
        c1.close()
        c2.close()


def _build_slo_results(metrics: RunMetrics) -> dict[str, dict[str, object]]:
    p95 = round(metrics.percentile(95), 2)
    recovery = metrics.recovery_time_ms
    return {
        "Availability": {"target": ">= 99%", "actual": round(metrics.availability, 4), "met": metrics.availability >= 0.99},
        "Latency P95": {"target": "< 2500 ms", "actual": p95, "met": p95 < 2500},
        "Fallback success rate": {
            "target": ">= 95%",
            "actual": round(metrics.fallback_success_rate, 4),
            "met": metrics.fallback_success_rate >= 0.95,
        },
        "Cache hit rate": {
            "target": ">= 10%",
            "actual": round(metrics.cache_hit_rate, 4),
            "met": metrics.cache_hit_rate >= 0.10,
        },
        "Recovery time": {
            "target": "< 5000 ms",
            "actual": None if recovery is None else round(recovery, 2),
            "met": recovery is not None and recovery < 5000,
        },
    }


def _default_scenarios() -> Iterable[ScenarioConfig]:
    return [ScenarioConfig(name="default", description="Baseline run")]


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run configured scenarios, cache comparison, Redis evidence, and SLO checks."""
    combined = RunMetrics()
    scenarios = config.scenarios if config.scenarios else list(_default_scenarios())

    for scenario in scenarios:
        result = run_scenario(config, queries, scenario)
        _merge_metrics(combined, result)

    combined.cache_comparison = run_cache_comparison(config, queries)
    combined.redis_shared_cache = verify_redis_shared_cache(config)
    combined.slo_results = _build_slo_results(combined)
    return combined
