"""Per-message phase timings, for the query log.

`duration_ms` on `query_processed` is end to end, so it cannot say where a slow answer spent its time or when the
visitor first saw text. `PhaseTimings` records how long each phase before the model took (`phase`) and when a
point in the message was reached, measured from when the agent began the message (`mark`).

Phases are recorded by duration, not as slices of one timeline: once phases overlap (run with `asyncio.gather`) the
durations add up to more than the elapsed time, and that is the point of the figure.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager


class PhaseTimings:
    def __init__(self, start: float | None = None):
        # `start` is a `time.perf_counter()` value; default is now
        self.start = time.perf_counter() if start is None else start
        self.phases_ms: dict[str, int] = {}
        self.marks_ms: dict[str, int] = {}

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Time the block. A phase entered twice is summed; one that raises is still recorded."""
        began = time.perf_counter()
        try:
            yield
        finally:
            elapsed = int((time.perf_counter() - began) * 1000)
            self.phases_ms[name] = self.phases_ms.get(name, 0) + elapsed

    def mark(self, name: str) -> None:
        """Record when `name` happened, in ms since the start. The first call for a name wins."""
        self.marks_ms.setdefault(name, int((time.perf_counter() - self.start) * 1000))

    def get_mark(self, name: str) -> int | None:
        return self.marks_ms.get(name)
