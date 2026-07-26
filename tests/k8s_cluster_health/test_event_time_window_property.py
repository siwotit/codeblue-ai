"""Property-based test for event time-window filtering.

**Property 10: Event time-window filtering**
**Validates: Requirement 3.6**

For any set of events with varying timestamps, verify only events within
last 1h are included, Normal events are excluded, and max 100 events returned.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

# Ensure the project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent.parent / "skills" / "k8s-cluster-health"),
)

from k8s_cluster_health import MAX_EVENTS, process_events


# --- Strategies ---

# Generate timestamps spanning a wide range: from 48h ago to 30min in the future
def timestamp_strategy():
    """Generate timestamps spanning wide range around the cutoff."""
    return st.floats(
        min_value=-48 * 3600,  # 48h before cutoff
        max_value=3600,  # 1h after cutoff (well within window)
        allow_nan=False,
        allow_infinity=False,
    ).map(lambda offset_secs: datetime.now(timezone.utc) + timedelta(seconds=offset_secs))


def iso_timestamp_from_dt(dt: datetime) -> str:
    """Convert datetime to ISO format string ending in Z."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


event_type_strategy = st.sampled_from(["Warning", "Normal"])


def event_strategy():
    """Generate a single K8s event dict with varying type and timestamp."""
    return st.fixed_dictionaries({
        "type": event_type_strategy,
        "reason": st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("L", "N"))),
        "involvedObject": st.fixed_dictionaries({
            "kind": st.sampled_from(["Pod", "Deployment", "Service", "Node"]),
            "namespace": st.sampled_from(["default", "kube-system", "payments", "api"]),
            "name": st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N", "Pd"))),
        }),
        "message": st.text(min_size=1, max_size=100),
        "count": st.integers(min_value=1, max_value=1000),
        "lastTimestamp": timestamp_strategy().map(iso_timestamp_from_dt),
        "firstTimestamp": timestamp_strategy().map(iso_timestamp_from_dt),
    })


# --- Property Test ---


@given(events=st.lists(event_strategy(), min_size=0, max_size=200))
@settings(max_examples=200, deadline=None)
def test_event_time_window_filtering_property(events: list[dict]):
    """Property 10: Event time-window filtering.

    **Validates: Requirement 3.6**

    For any set of events with varying timestamps:
    1. No events with timestamps before the cutoff are included
    2. Events with timestamps after the cutoff are included (if Warning type)
    3. Maximum of 100 events are returned
    4. Normal type events are excluded regardless of timestamp
    """
    # Use a fixed cutoff: 1 hour ago from now
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)

    result = process_events(events, cutoff)

    # Property 1: Maximum of MAX_EVENTS (100) events returned
    assert len(result) <= MAX_EVENTS

    # Property 2: All returned events are Warning type
    for event_summary in result:
        assert event_summary.type.value == "Warning", (
            f"Non-Warning event found in results: {event_summary.type}"
        )

    # Property 3: No returned event has a lastTimestamp before the cutoff
    for event_summary in result:
        assert event_summary.last_timestamp >= cutoff, (
            f"Event with timestamp {event_summary.last_timestamp} is before cutoff {cutoff}"
        )

    # Property 4: All Warning events with lastTimestamp >= cutoff should be in results
    # (up to the MAX_EVENTS limit)
    expected_included = []
    for event in events:
        if event.get("type") != "Warning":
            continue
        last_ts_str = event.get("lastTimestamp", "")
        if not last_ts_str:
            continue
        # Parse the timestamp the same way the function does
        ts_str = last_ts_str
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        try:
            last_ts = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            continue
        if last_ts >= cutoff:
            expected_included.append(event)

    # The number of results should be min(eligible_events, MAX_EVENTS)
    assert len(result) == min(len(expected_included), MAX_EVENTS), (
        f"Expected {min(len(expected_included), MAX_EVENTS)} events but got {len(result)}. "
        f"Eligible events: {len(expected_included)}"
    )


def normal_event_strategy():
    """Generate a Normal event directly (no filtering needed)."""
    return st.fixed_dictionaries({
        "type": st.just("Normal"),
        "reason": st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("L", "N"))),
        "involvedObject": st.fixed_dictionaries({
            "kind": st.sampled_from(["Pod", "Deployment", "Service", "Node"]),
            "namespace": st.sampled_from(["default", "kube-system", "payments", "api"]),
            "name": st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N", "Pd"))),
        }),
        "message": st.text(min_size=1, max_size=100),
        "count": st.integers(min_value=1, max_value=1000),
        "lastTimestamp": timestamp_strategy().map(iso_timestamp_from_dt),
        "firstTimestamp": timestamp_strategy().map(iso_timestamp_from_dt),
    })


@given(events=st.lists(normal_event_strategy(), min_size=1, max_size=50))
@settings(max_examples=50, deadline=None)
def test_normal_events_always_excluded(events: list[dict]):
    """Normal type events are never included regardless of timestamp.

    **Validates: Requirement 3.6**
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    result = process_events(events, cutoff)
    assert len(result) == 0, "Normal events should never appear in filtered results"


def warning_event_strategy():
    """Generate a Warning event directly (no filtering needed)."""
    return st.fixed_dictionaries({
        "type": st.just("Warning"),
        "reason": st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("L", "N"))),
        "involvedObject": st.fixed_dictionaries({
            "kind": st.sampled_from(["Pod", "Deployment", "Service", "Node"]),
            "namespace": st.sampled_from(["default", "kube-system", "payments", "api"]),
            "name": st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N", "Pd"))),
        }),
        "message": st.text(min_size=1, max_size=100),
        "count": st.integers(min_value=1, max_value=1000),
        "lastTimestamp": timestamp_strategy().map(iso_timestamp_from_dt),
        "firstTimestamp": timestamp_strategy().map(iso_timestamp_from_dt),
    })


@given(events=st.lists(warning_event_strategy(), min_size=101, max_size=200))
@settings(max_examples=50, deadline=None)
def test_max_events_cap_enforced(events: list[dict]):
    """When more than 100 eligible Warning events exist, cap at 100.

    **Validates: Requirement 3.6**
    """
    # Use a cutoff far in the past so all events are within the window
    cutoff = datetime.now(timezone.utc) - timedelta(hours=72)
    result = process_events(events, cutoff)
    assert len(result) <= MAX_EVENTS, (
        f"Result count {len(result)} exceeds MAX_EVENTS {MAX_EVENTS}"
    )
