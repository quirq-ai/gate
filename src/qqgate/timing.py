"""V0-GAT-04: when did a change enter the gate? Stamped on each gate run so the scorecard can report
queue-entry-to-verdict p50 and p90 per repo (scorecard v0 in test-pipelines reads Run.queued_at).

The backend finds the queue-entry time; this module only checks and formats it.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from qqgate.errors import GateError


@dataclass(frozen=True)
class QueuedAt:
    at: str          # RFC 3339 UTC, second precision: 2026-10-04T12:00:00Z
    source: str      # where it came from, e.g. "timeline:added_to_merge_queue"
    exact: bool      # False when only an approximation was available


def rfc3339(value: str) -> str:
    """Normalize a timestamp to RFC 3339 UTC with a Z, or raise GateError."""
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise GateError(f"not an RFC 3339 timestamp: {value!r}") from None
    if t.tzinfo is None:
        raise GateError(f"timestamp has no time zone: {value!r}")
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
