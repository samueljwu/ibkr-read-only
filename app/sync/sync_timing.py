"""Conservative time estimates for live syncs and dated report imports."""

from __future__ import annotations

import math
from datetime import datetime


def duration_text(seconds: float, *, approximate=False) -> str:
    seconds = max(1, math.ceil(seconds))
    if approximate:
        if seconds < 10:
            return "<10s"
        seconds = math.ceil(seconds / (10 if seconds < 60 else 60)) * (10 if seconds < 60 else 60)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = ([f"{hours}h"] if hours else []) + ([f"{minutes}m"] if minutes else [])
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return ("~" if approximate else "") + " ".join(parts)


def remaining_text(seconds: float | None, *, waiting=False) -> str:
    if waiting or (seconds is not None and seconds <= 0):
        return "Waiting…"
    if seconds is None:
        return "Estimating…"
    return f"{duration_text(seconds, approximate=True)} remaining"


class SyncTiming:
    """Learn only successful stage durations; live and Flex work can overlap."""

    def __init__(self, durations=None):
        self.durations = {}
        if isinstance(durations, dict):
            for key, value in durations.items():
                if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
                    self.durations[key] = float(value)
        self.started = {}

    def start(self, key, now):
        self.started[key] = now

    def finish(self, key, now, *, success):
        started = self.started.pop(key, None)
        if started is not None and success:
            elapsed = max(0.1, now - started)
            previous = self.durations.get(key)
            self.durations[key] = elapsed if previous is None else 0.7 * previous + 0.3 * elapsed

    def estimate(self, live, sequential, now):
        keys = ([live] if live else []) + sequential
        # An overdue stage has no reliable countdown, even with other work queued.
        if any(key in self.started and
               now - self.started[key] >= self.durations.get(key, 30) for key in keys):
            return remaining_text(None, waiting=True)
        if not keys or any(key not in self.durations for key in keys):
            return remaining_text(None)
        def remaining(key):
            elapsed = now - self.started[key] if key in self.started else 0
            return max(0, self.durations[key] - elapsed)
        return remaining_text(max(remaining(live) if live else 0,
                                  sum(remaining(key) for key in sequential)))


def history_remaining(status, now):
    """Use complete scheduled batches, including any split recovery requests."""
    total = status.get("total_batches", 0)
    completed = status.get("processed_batches", 0)
    sampled = status.get("timed_batches", 0)
    elapsed = status.get("batch_seconds", 0)
    updated = status.get("batch_finished_at")
    if not sampled or not elapsed or not updated:
        try:
            started = datetime.fromisoformat(status["batch_started_at"])
            if (now - started).total_seconds() >= 30:
                return remaining_text(None, waiting=True)
        except (KeyError, TypeError, ValueError):
            pass
        return remaining_text(None)
    try:
        since_update = max(0, (now - datetime.fromisoformat(updated)).total_seconds())
    except (TypeError, ValueError):
        return remaining_text(None)
    mean = elapsed / sampled
    if completed >= total or since_update >= max(30, mean * 2):
        return remaining_text(None, waiting=True)
    return remaining_text(mean * (total - completed) - since_update)
