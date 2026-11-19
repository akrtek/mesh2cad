"""Progress reporting for long conversions.

A `Progress` maps nested stage ranges onto one 0..1 value:

    with prog.part(0.10, 0.60, "Finding curved surfaces"):
        for i, item in enumerate(items):
            prog.set(i / len(items), f"{i:,} of {len(items):,} regions")

Values only ever move forward, updates are throttled, and the time spent in each
named stage is recorded in `timings` (used for the per-body timing line in the log).
"""
from __future__ import annotations

import time
from contextlib import contextmanager


class Progress:
    def __init__(self, emit=None, interval: float = 0.25):
        self.emit = emit                  # callable(value: float, text: str) or None
        self.interval = interval
        self._stack = [(0.0, 1.0, "")]    # (absolute lo, absolute hi, label)
        self.value = 0.0
        self._last = 0.0
        self.timings: dict[str, float] = {}
        self.prefix = ""                  # e.g. "Body 2 of 6 · " for multi-body files

    # ------------------------------------------------------------------ ranges
    def local(self) -> float:
        """Current value as a fraction of the innermost range."""
        lo, hi, _ = self._stack[-1]
        return 0.0 if hi <= lo else min(max((self.value - lo) / (hi - lo), 0.0), 1.0)

    @contextmanager
    def part(self, a: float, b: float, label: str | None = None):
        """Run a stage that occupies [a, b] of the current range."""
        lo, hi, parent = self._stack[-1]
        self._stack.append((lo + (hi - lo) * a, lo + (hi - lo) * b, label or parent))
        t0 = time.monotonic()
        self.set(0.0, force=True)
        try:
            yield self
        finally:
            self.set(1.0)
            self._stack.pop()
            if label:
                self.timings[label] = self.timings.get(label, 0.0) + time.monotonic() - t0

    # ------------------------------------------------------------------ updates
    def set(self, f: float, detail: str | None = None, force: bool = False) -> None:
        lo, hi, label = self._stack[-1]
        self.value = max(self.value, lo + (hi - lo) * min(max(float(f), 0.0), 1.0))
        if self.emit is None:
            return
        now = time.monotonic()
        if not force and now - self._last < self.interval:
            return
        text = " · ".join(x for x in (label, detail) if x)
        text = self.prefix + text if text else ""
        if (round(self.value, 4), text) == getattr(self, "_sent", None):
            return
        self._sent = (round(self.value, 4), text)
        self._last = now
        self.emit(self.value, text)

    def say(self, detail: str) -> None:
        """Change the detail text without moving the bar."""
        self.set(self.local(), detail, force=True)

    def reset_timings(self) -> None:
        self.timings = {}

    def timing_line(self, order) -> str:
        parts = [f"{k.lower()} {self.timings[k]:.1f}s" for k in order if self.timings.get(k, 0) >= 0.05]
        return ", ".join(parts)


NULL = Progress(None)
