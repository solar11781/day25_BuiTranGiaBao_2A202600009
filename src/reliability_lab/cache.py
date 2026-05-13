from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from threading import RLock
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — used by both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit\s*card|credit.card|ssn|social\s*security|"
    r"social.security|user\s*\d+|user.\d+|account\s*\d+|account.\d+)\b",
    re.IGNORECASE,
)
TOKEN_PATTERN = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def _is_uncacheable(query: str) -> bool:
    """Return True if a query should not be cached for safety/privacy reasons."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


def _tokens(value: str) -> list[str]:
    return TOKEN_PATTERN.findall(value.lower())


def _char_ngrams(value: str, n: int = 3) -> set[str]:
    normalized = " ".join(_tokens(value))
    if not normalized:
        return set()
    if len(normalized) <= n:
        return {normalized}
    return {normalized[i : i + n] for i in range(len(normalized) - n + 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


# ---------------------------------------------------------------------------
# In-memory cache (kept for comparison/testing; Redis is the default config)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """Small TTL cache with exact-match, similarity lookup, and safety guardrails."""

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.false_hit_log: list[dict[str, object]] = []
        self._entries: list[CacheEntry] = []
        self._lock: Any = RLock()

    def get(self, query: str) -> tuple[str | None, float]:
        if _is_uncacheable(query):
            return None, 0.0

        now = time.time()
        exact_key = query.lower().strip()
        best_entry: CacheEntry | None = None
        best_score = 0.0

        with self._lock:
            self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]
            for entry in self._entries:
                if entry.key.lower().strip() == exact_key:
                    return entry.value, 1.0

                score = self.similarity(query, entry.key)
                if score > best_score:
                    best_score = score
                    best_entry = entry

        if best_entry is not None and best_score >= self.similarity_threshold:
            if _looks_like_false_hit(query, best_entry.key):
                self.false_hit_log.append(
                    {"query": query, "cached_query": best_entry.key, "score": round(best_score, 4)}
                )
                return None, best_score
            return best_entry.value, best_score
        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        if _is_uncacheable(query):
            return
        with self._lock:
            self._entries.append(CacheEntry(query, value, time.time(), metadata or {}))

    def flush(self) -> None:
        with self._lock:
            self._entries.clear()
            self.false_hit_log.clear()

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Deterministic lexical similarity with exact-match and n-gram fallback.

        This avoids external APIs while being stricter than plain token overlap.
        The false-hit guard in get() blocks date-sensitive near matches such as
        "refund policy for 2024" vs. "refund policy for 2026".
        """
        norm_a = " ".join(_tokens(a))
        norm_b = " ".join(_tokens(b))
        if not norm_a or not norm_b:
            return 0.0
        if norm_a == norm_b:
            return 1.0

        token_score = _jaccard(set(norm_a.split()), set(norm_b.split()))
        char_score = _jaccard(_char_ngrams(norm_a), _char_ngrams(norm_b))
        return max(token_score, char_score * 0.95)


# ---------------------------------------------------------------------------
# Redis shared cache
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments."""

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis.

        The lookup tries exact hash match first, then scans the cache namespace for
        the best similar query. Redis errors are treated as cache misses so the
        gateway can keep serving provider/fallback responses.
        """
        if _is_uncacheable(query):
            return None, 0.0

        try:
            exact_key = f"{self.prefix}{self._query_hash(query)}"
            exact_response = self._redis.hget(exact_key, "response")
            if isinstance(exact_response, str):
                return exact_response, 1.0

            best_key: str | None = None
            best_query: str | None = None
            best_response: str | None = None
            best_score = 0.0

            for key in self._redis.scan_iter(f"{self.prefix}*"):
                cached_query = self._redis.hget(key, "query")
                cached_response = self._redis.hget(key, "response")
                if not isinstance(cached_query, str) or not isinstance(cached_response, str):
                    continue
                score = ResponseCache.similarity(query, cached_query)
                if score > best_score:
                    best_key = str(key)
                    best_query = cached_query
                    best_response = cached_response
                    best_score = score

            if (
                best_key is not None
                and best_query is not None
                and best_response is not None
                and best_score >= self.similarity_threshold
            ):
                if _looks_like_false_hit(query, best_query):
                    self.false_hit_log.append(
                        {
                            "query": query,
                            "cached_query": best_query,
                            "redis_key": best_key,
                            "score": round(best_score, 4),
                        }
                    )
                    return None, best_score
                return best_response, best_score
            return None, best_score
        except Exception:
            return None, 0.0

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with a TTL."""
        if _is_uncacheable(query):
            return
        try:
            key = f"{self.prefix}{self._query_hash(query)}"
            mapping: dict[str, str] = {"query": query, "response": value}
            if metadata:
                mapping.update({f"meta:{k}": str(v) for k, v in metadata.items()})
            self._redis.hset(key, mapping=mapping)
            self._redis.expire(key, self.ttl_seconds)
        except Exception:
            return

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing/reproducible runs)."""
        try:
            for key in self._redis.scan_iter(f"{self.prefix}*"):
                self._redis.delete(key)
            self.false_hit_log.clear()
        except Exception:
            return

    def keys(self) -> list[str]:
        """Return cache keys visible in Redis for report evidence."""
        try:
            return [str(key) for key in self._redis.scan_iter(f"{self.prefix}*")]
        except Exception:
            return []

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
