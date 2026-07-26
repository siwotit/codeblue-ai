"""Property test for bounded correlation scores.

**Validates: Requirement 4.5**

Property 4: Bounded numeric scores (correlation)
For any discovered change, verify 0.0 <= correlationScore <= 1.0.
Also verifies that temporal_proximity is always 0.0-1.0 for any valid inputs.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "skills"))
sys.path.insert(
    0,
    str(__import__("pathlib").Path(__file__).resolve().parents[2] / "skills" / "deploy-correlation"),
)

from hypothesis import given, settings
from hypothesis import strategies as st

import deploy_correlation as dc


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Temporal proximity values in valid range (0.0-1.0)
proximity_strategy = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)

# Lookback window in seconds — positive values representing realistic lookback
# windows (1 second to 7 days)
lookback_seconds_strategy = st.floats(
    min_value=0.0, max_value=604800.0, allow_nan=False, allow_infinity=False
)

# Time delta in seconds — any non-negative offset from incident time
time_delta_strategy = st.floats(
    min_value=0.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False
)


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


@given(
    temporal_proximity=proximity_strategy,
    resource_overlap=st.booleans(),
)
@settings(max_examples=500)
def test_correlation_score_bounded(
    temporal_proximity: float,
    resource_overlap: bool,
) -> None:
    """For any valid temporal proximity and overlap, correlationScore is in [0.0, 1.0].

    **Validates: Requirement 4.5**

    The Deploy_Correlation skill SHALL produce a correlation score between 0.0
    and 1.0 inclusive for every discovered change.
    """
    score = dc.compute_correlation_score(temporal_proximity, resource_overlap)

    assert 0.0 <= score <= 1.0, (
        f"Correlation score {score} out of bounds for "
        f"temporal_proximity={temporal_proximity}, resource_overlap={resource_overlap}"
    )


@given(
    time_delta_seconds=time_delta_strategy,
    lookback_seconds=lookback_seconds_strategy,
)
@settings(max_examples=500)
def test_temporal_proximity_bounded(
    time_delta_seconds: float,
    lookback_seconds: float,
) -> None:
    """For any valid change time and lookback window, temporal proximity is in [0.0, 1.0].

    **Validates: Requirement 4.5**

    Temporal proximity is an intermediate value feeding into the correlation
    score. It must always be bounded to ensure the final score stays bounded.
    """
    incident_time = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    change_time = incident_time - timedelta(seconds=time_delta_seconds)

    proximity = dc.compute_temporal_proximity(change_time, incident_time, lookback_seconds)

    assert 0.0 <= proximity <= 1.0, (
        f"Temporal proximity {proximity} out of bounds for "
        f"time_delta_seconds={time_delta_seconds}, lookback_seconds={lookback_seconds}"
    )


@given(
    time_delta_seconds=time_delta_strategy,
    lookback_seconds=st.floats(
        min_value=1.0, max_value=604800.0, allow_nan=False, allow_infinity=False
    ),
    resource_overlap=st.booleans(),
)
@settings(max_examples=500)
def test_end_to_end_score_bounded(
    time_delta_seconds: float,
    lookback_seconds: float,
    resource_overlap: bool,
) -> None:
    """End-to-end: computing proximity then correlation score stays in [0.0, 1.0].

    **Validates: Requirement 4.5**

    Combines temporal proximity computation and correlation score computation
    to verify the full pipeline always produces bounded outputs.
    """
    incident_time = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    change_time = incident_time - timedelta(seconds=time_delta_seconds)

    # Compute temporal proximity
    proximity = dc.compute_temporal_proximity(change_time, incident_time, lookback_seconds)
    assert 0.0 <= proximity <= 1.0

    # Compute correlation score from bounded proximity
    score = dc.compute_correlation_score(proximity, resource_overlap)
    assert 0.0 <= score <= 1.0, (
        f"End-to-end correlation score {score} out of bounds for "
        f"proximity={proximity}, resource_overlap={resource_overlap}"
    )
