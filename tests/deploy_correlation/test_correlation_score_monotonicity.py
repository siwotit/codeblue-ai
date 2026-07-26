"""Property test for correlation score monotonicity.

**Validates: Requirements 4.6**

Property 7: For any two changes where the second has greater proximity AND
resource overlap, the second's correlation score must be >= the first's score.

Also verifies: score with both factors (proximity + overlap) > score with only
proximity (overlap=False).
"""

from __future__ import annotations

import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "skills"))
sys.path.insert(
    0,
    str(__import__("pathlib").Path(__file__).resolve().parents[2] / "skills" / "deploy-correlation"),
)

from hypothesis import given, settings
from hypothesis import strategies as st

import deploy_correlation as dc


# Strategy for valid temporal proximity values (0.0 to 1.0)
proximity_strategy = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)


@given(
    proximity1=proximity_strategy,
    proximity2=proximity_strategy,
    overlap1=st.booleans(),
)
@settings(max_examples=500)
def test_correlation_score_monotonicity_greater_proximity_and_overlap(
    proximity1: float,
    proximity2: float,
    overlap1: bool,
) -> None:
    """Property 7: When proximity2 > proximity1 AND overlap2=True, score2 >= score1.

    **Validates: Requirements 4.6**

    For any two changes where the second has greater temporal proximity AND
    resource overlap is present, the second change's correlation score must be
    at least as high as the first change's score regardless of the first
    change's overlap status.
    """
    # Filter: proximity2 must be strictly greater than proximity1
    if proximity2 <= proximity1:
        return  # Skip — precondition not met

    score1 = dc.compute_correlation_score(proximity1, overlap1)
    score2 = dc.compute_correlation_score(proximity2, True)

    assert score2 >= score1, (
        f"Monotonicity violated: "
        f"score({proximity2}, True)={score2} < score({proximity1}, {overlap1})={score1}"
    )


@given(proximity=st.floats(min_value=0.01, max_value=1.0, allow_nan=False, allow_infinity=False))
@settings(max_examples=500)
def test_both_factors_greater_than_proximity_alone(proximity: float) -> None:
    """Score with both factors must exceed score with only proximity.

    **Validates: Requirements 4.6**

    When both temporal proximity is non-trivial and resource overlap is present,
    the correlation score must be strictly higher than when only temporal
    proximity is present (overlap=False).
    """
    score_both = dc.compute_correlation_score(proximity, True)
    score_proximity_only = dc.compute_correlation_score(proximity, False)

    assert score_both > score_proximity_only, (
        f"Both-factors score ({score_both}) should exceed proximity-only score "
        f"({score_proximity_only}) for proximity={proximity}"
    )


@given(
    low_proximity=st.floats(min_value=0.0, max_value=0.99, allow_nan=False, allow_infinity=False),
    delta=st.floats(min_value=0.001, max_value=1.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=500)
def test_correlation_score_monotonic_with_overlap_fixed(
    low_proximity: float,
    delta: float,
) -> None:
    """With overlap=True, increasing proximity must not decrease the score.

    **Validates: Requirements 4.6**

    This verifies the monotonic relationship: for overlap=True,
    score(proximity + delta, True) >= score(proximity, True).
    """
    high_proximity = low_proximity + delta
    if high_proximity > 1.0:
        return  # Skip — out of valid range

    score_low = dc.compute_correlation_score(low_proximity, True)
    score_high = dc.compute_correlation_score(high_proximity, True)

    assert score_high >= score_low, (
        f"Monotonicity violated with overlap=True: "
        f"score({high_proximity}, True)={score_high} < score({low_proximity}, True)={score_low}"
    )
