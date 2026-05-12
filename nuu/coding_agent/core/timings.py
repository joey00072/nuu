"""
Timing and performance tracking utilities. Measures and reports elapsed time
for LLM calls, tool executions, and other agent operations.

Owns: timing collectors and reporters.
Delegates to: time for measurement, os for environment checks.

Depends on: standard library only (os, time)
"""

from __future__ import annotations

import os
import time


ENABLED = os.environ.get("NUU_TIMING") == "1"


class Timings:
    def __init__(self) -> None:
        self._phases: dict[str, float] = {}
        self._last: float = time.monotonic()

    def start(self, phase: str) -> None:
        if not ENABLED:
            return
        self._last = time.monotonic()

    def end(self, phase: str) -> None:
        if not ENABLED:
            return
        elapsed = time.monotonic() - self._last
        self._phases[phase] = elapsed

    def get_total(self) -> float:
        return sum(self._phases.values())

    def print(self) -> None:
        if not ENABLED or not self._phases:
            return
        print("\n--- Startup Timings ---")
        for label, elapsed in self._phases.items():
            print(f"  {label}: {elapsed * 1000:.0f}ms")
        print(f"  TOTAL: {self.get_total() * 1000:.0f}ms")
        print("------------------------\n")
