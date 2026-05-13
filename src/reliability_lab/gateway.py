from __future__ import annotations

import time
from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None
    route_reason: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache

    def complete(self, prompt: str) -> GatewayResponse:
        """Return a reliable response or a static fallback."""
        start = time.perf_counter()
        errors: list[str] = []

        if self.cache is not None:
            cached, score = self.cache.get(prompt)
            if cached is not None:
                latency_ms = (time.perf_counter() - start) * 1000
                return GatewayResponse(
                    text=cached,
                    route="cache_hit",
                    provider=None,
                    cache_hit=True,
                    latency_ms=latency_ms,
                    estimated_cost=0.0,
                    route_reason=f"cache_hit:score={score:.2f}",
                )

        for index, provider in enumerate(self.providers):
            breaker = self.breakers[provider.name]
            provider_role = "primary" if index == 0 else "fallback"
            try:
                response: ProviderResponse = breaker.call(provider.complete, prompt)
                if self.cache is not None:
                    self.cache.set(prompt, response.text, {"provider": provider.name})
                latency_ms = (time.perf_counter() - start) * 1000
                return GatewayResponse(
                    text=response.text,
                    route=provider_role,
                    provider=provider.name,
                    cache_hit=False,
                    latency_ms=latency_ms,
                    estimated_cost=response.estimated_cost,
                    route_reason=f"{provider_role}:{provider.name}:served",
                )
            except CircuitOpenError as exc:
                errors.append(f"{provider.name}:circuit_open:{exc}")
                continue
            except ProviderError as exc:
                errors.append(f"{provider.name}:provider_error:{exc}")
                continue

        latency_ms = (time.perf_counter() - start) * 1000
        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=latency_ms,
            estimated_cost=0.0,
            error="; ".join(errors) if errors else "no providers configured",
            route_reason="static_fallback:all_providers_unavailable",
        )
