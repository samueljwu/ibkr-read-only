"""Durable progress for a dated history import.

The worker writes this file after every event so a completed or failed import
always leaves a result for the GUI, even when the console is gone.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4


def _defaults():
    return {
        "state": "starting", "pid": None, "started_sources": [],
        "completed_sources": [], "completed_windows": {}, "unavailable_windows": {},
        "unverified_windows": {}, "outside_history_windows": {}, "weekend_windows": {},
        "updated_windows": {}, "unchanged_windows": {}, "review_windows": {}, "cash_boundary_periods": {},
        "stopped_after_unavailable": False, "active_source": None,
        "current_window": None, "failed_window": None,
        "source": "", "from_date": "", "to_date": "", "message": "", "finished_at": None,
        "legacy_result": None,
    }


def read_status(path):
    """Return the saved status, merged over defaults; missing or unreadable files yield defaults."""
    status = _defaults()
    try:
        saved = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return status
    if isinstance(saved, dict):
        status.update(saved)
    return status


def pause_request_path(status_path):
    """Separate control file so worker progress writes cannot erase a pause request."""
    return Path(status_path).with_suffix(".pause")


def write_status(path, status):
    """Write the status atomically so a reader never sees a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}-{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(status, indent=2, sort_keys=True), encoding="utf-8")
        for attempt in range(3):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _spans(status, key, source):
    return status.get(key, {}).get(source) or []


def merge_periods(periods_by_source):
    """Merge adjacent/overlapping date windows per source into sorted (first, last) spans.

    Returns {source: [(date, date), ...]} for the fixed source order trades/analytics/cash,
    keeping the exact merge rule (periods touching or overlapping by a day join together)
    that format_imported_periods previously implemented inline.
    """
    merged_by_source = {}
    for source in ("trades", "analytics", "cash"):
        spans = sorted((date.fromisoformat(first), date.fromisoformat(last))
                       for first, last in periods_by_source.get(source, ()))
        merged = []
        for first, last in spans:
            if merged and first <= merged[-1][1] + timedelta(days=1):
                merged[-1] = (merged[-1][0], max(last, merged[-1][1]))
            else:
                merged.append((first, last))
        merged_by_source[source] = merged
    return merged_by_source


def format_imported_periods(periods_by_source):
    """Summarize saved complete Flex windows, preserving gaps between periods."""
    lines = []
    merged_by_source = merge_periods(periods_by_source)
    for source in ("trades", "analytics", "cash"):
        merged = merged_by_source[source]
        periods = ", ".join(f"{first.isoformat()} to {last.isoformat()}"
                            for first, last in merged)
        lines.append(f"{source.capitalize()}: {periods or 'none'}")
    return "Imported periods (all runs):\n" + "\n".join(lines)


def format_history_details(status):
    """Human-readable per-source summary of what each import window did."""
    if status.get("source") == "legacy":
        result = status.get("legacy_result") or {}
        lines = []
        if status.get("action") == "remove":
            if result:
                lines.append(f"Removed {result['months']} monthly observations, "
                             f"{result['statements']} Activity Statements, and "
                             f"{result['opening_positions']} closing stock quantity anchors.")
            if status.get("message"):
                lines.append(status["message"])
            return "\n".join(lines)
        if result:
            lines.append(f"Older monthly history: {result['period_from']} to {result['period_to']}")
            lines.append(f"Monthly NAV observations: {result['months']} ({result['new_months']} new)")
            lines.append(f"Activity Statement evidence rows: {result['evidence_rows']}")
            if "opening_positions" in result:
                lines.append(f"Closing stock quantities: {result['opening_positions']} "
                             f"({result['new_opening_positions']} new anchors)")
            if result.get("new_performance"):
                lines.append("PortfolioAnalyst since-inception return saved.")
            lines.append("Daily analysis is unavailable for this period.")
        if status.get("state") in ("failed", "needs_review"):
            lines.append("Import needs review." if status["state"] == "needs_review" else "Import failed.")
        if status.get("message"):
            lines.append(status["message"])
        return "\n".join(lines)
    lines = []
    for source in status.get("started_sources") or []:
        classified = (_spans(status, "updated_windows", source) +
                      _spans(status, "unchanged_windows", source) +
                      _spans(status, "review_windows", source))
        groups = (
            ("Updated", _spans(status, "updated_windows", source)),
            ("Unchanged", _spans(status, "unchanged_windows", source)),
            ("Needs review", _spans(status, "review_windows", source)),
            ("Imported", [span for span in _spans(status, "completed_windows", source) if span not in classified]),
            ("Whole Cash boundary period", _spans(status, "cash_boundary_periods", source)),
            ("Not available", _spans(status, "unavailable_windows", source)),
            ("Not requested", _spans(status, "unverified_windows", source)),
            ("Weekend end not requested", _spans(status, "weekend_windows", source)),
            ("Outside IBKR history", _spans(status, "outside_history_windows", source)),
        )
        if not any(spans for _, spans in groups):
            continue
        lines.append(f"{source.capitalize()}:")
        for label, spans in groups:
            for span in spans:
                lines.append(f"  {label}: {span[0]} to {span[1]}")
    state = status.get("state")
    if state == "completed_with_gaps":
        lines.append("Import completed with gaps.")
        if any(status.get(key) for key in ("unverified_windows", "outside_history_windows")):
            lines.append("IBKR did not specify the cause for every gap; review the sources above.")
    elif state == "completed":
        lines.append("Import completed.")
    elif state == "paused":
        lines.append("Import paused. Completed windows are saved; resume to request the rest.")
    elif state == "interrupted":
        lines.append("Import interrupted when the app closed. Completed windows remain saved.")
        span = status.get("failed_window")
        if span:
            lines.append(f"Interrupted window: {span[0]} to {span[1]}.")
    elif state in ("failed", "needs_review"):
        lines.append("Import needs review." if state == "needs_review" else "Import failed.")
        source = status.get("active_source")
        span = status.get("failed_window") or status.get("current_window")
        if source:
            lines.append(f"Failed source: {source.capitalize()}.")
        if span:
            lines.append(f"Failed window: {span[0]} to {span[1]}.")
    else:
        lines.append(f"Import state: {state or 'unknown'}.")
    message = status.get("message")
    if message and state in ("failed", "needs_review"):
        lines.append(message)
    return "\n".join(lines)
