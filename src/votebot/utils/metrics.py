"""Performance metrics collection and reporting."""

import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Generator

import structlog

logger = structlog.get_logger()


@dataclass
class MetricValue:
    """A single metric measurement."""

    value: float
    timestamp: datetime = field(default_factory=datetime.utcnow)
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class MetricSummary:
    """Summary statistics for a metric."""

    name: str
    count: int
    min: float
    max: float
    avg: float
    p50: float
    p95: float
    p99: float


class MetricsCollector:
    """
    Collect and report application metrics.

    Tracks:
    - Request latencies
    - Token usage
    - Error rates
    - Cache hit rates
    """

    def __init__(self, retention_minutes: int = 60):
        """
        Initialize the metrics collector.

        Args:
            retention_minutes: How long to retain metrics
        """
        self.retention = timedelta(minutes=retention_minutes)
        self._metrics: dict[str, list[MetricValue]] = defaultdict(list)
        self._counters: dict[str, int] = defaultdict(int)

    def record(
        self,
        name: str,
        value: float,
        tags: dict[str, str] | None = None,
    ) -> None:
        """
        Record a metric value.

        Args:
            name: Metric name
            value: Metric value
            tags: Optional tags for the metric
        """
        metric = MetricValue(value=value, tags=tags or {})
        self._metrics[name].append(metric)

        # Clean old metrics periodically
        if len(self._metrics[name]) % 100 == 0:
            self._cleanup(name)

    def increment(self, name: str, value: int = 1) -> None:
        """
        Increment a counter.

        Args:
            name: Counter name
            value: Amount to increment
        """
        self._counters[name] += value

    def get_counter(self, name: str) -> int:
        """Get the current value of a counter."""
        return self._counters[name]

    @contextmanager
    def timer(
        self,
        name: str,
        tags: dict[str, str] | None = None,
    ) -> Generator[None, None, None]:
        """
        Context manager to time an operation.

        Args:
            name: Metric name for the timing
            tags: Optional tags

        Yields:
            None
        """
        start = time.perf_counter()
        try:
            yield
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            self.record(name, duration_ms, tags)

    def get_summary(self, name: str) -> MetricSummary | None:
        """
        Get summary statistics for a metric.

        Args:
            name: Metric name

        Returns:
            MetricSummary or None if no data
        """
        self._cleanup(name)
        values = [m.value for m in self._metrics.get(name, [])]

        if not values:
            return None

        sorted_values = sorted(values)
        count = len(sorted_values)

        return MetricSummary(
            name=name,
            count=count,
            min=sorted_values[0],
            max=sorted_values[-1],
            avg=sum(sorted_values) / count,
            p50=self._percentile(sorted_values, 50),
            p95=self._percentile(sorted_values, 95),
            p99=self._percentile(sorted_values, 99),
        )

    def get_all_summaries(self) -> dict[str, MetricSummary]:
        """Get summaries for all metrics."""
        summaries = {}
        for name in list(self._metrics.keys()):
            summary = self.get_summary(name)
            if summary:
                summaries[name] = summary
        return summaries

    def get_report(self) -> dict[str, Any]:
        """
        Get a full metrics report.

        Returns:
            Dict with metrics and counters
        """
        summaries = self.get_all_summaries()

        return {
            "timestamp": datetime.utcnow().isoformat(),
            "metrics": {
                name: {
                    "count": s.count,
                    "min": round(s.min, 2),
                    "max": round(s.max, 2),
                    "avg": round(s.avg, 2),
                    "p50": round(s.p50, 2),
                    "p95": round(s.p95, 2),
                    "p99": round(s.p99, 2),
                }
                for name, s in summaries.items()
            },
            "counters": dict(self._counters),
        }

    def _cleanup(self, name: str) -> None:
        """Remove metrics older than retention period."""
        cutoff = datetime.utcnow() - self.retention
        self._metrics[name] = [
            m for m in self._metrics[name] if m.timestamp > cutoff
        ]

    def _percentile(self, sorted_values: list[float], percentile: int) -> float:
        """Calculate percentile from sorted values."""
        if not sorted_values:
            return 0.0
        k = (len(sorted_values) - 1) * percentile / 100
        f = int(k)
        c = f + 1 if f + 1 < len(sorted_values) else f
        return sorted_values[f] + (k - f) * (sorted_values[c] - sorted_values[f])

    def reset(self) -> None:
        """Reset all metrics and counters."""
        self._metrics.clear()
        self._counters.clear()


# Global metrics instance
_metrics: MetricsCollector | None = None


def get_metrics() -> MetricsCollector:
    """Get the global metrics collector instance."""
    global _metrics
    if _metrics is None:
        _metrics = MetricsCollector()
    return _metrics


# Convenience functions
def record_latency(operation: str, duration_ms: float) -> None:
    """Record a latency metric."""
    get_metrics().record(f"latency.{operation}", duration_ms)


def record_tokens(operation: str, tokens: int) -> None:
    """Record token usage."""
    get_metrics().record(f"tokens.{operation}", tokens)


def increment_request_count(endpoint: str, status: str) -> None:
    """Increment request counter."""
    get_metrics().increment(f"requests.{endpoint}.{status}")


def increment_error_count(error_type: str) -> None:
    """Increment error counter."""
    get_metrics().increment(f"errors.{error_type}")
