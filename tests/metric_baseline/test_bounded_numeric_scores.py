"""Property-based test: Bounded numeric scores.

**Validates: Requirements 2.6, 4.5, 6.3**

For any metric deviation result, verify 0.0 <= confidence <= 1.0.
Also verifies that confidence <= 0.5 when data points < 288 (sparse baseline per Req 2.8).
"""

from __future__ import annotations

import sys

from hypothesis import given, settings, assume
from hypothesis import strategies as st

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "skills"))
sys.path.insert(
    0,
    str(
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "skills"
        / "metric-baseline"
    ),
)

from metric_baseline import compute_confidence


# Strategy for baseline windows
baseline_window_strategy = st.sampled_from(["7d", "30d"])

# Strategy for baseline data points: lists of floats representing metric values.
# We use finite floats to avoid NaN/inf which are not valid metric values.
baseline_data_points_strategy = st.lists(
    st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False),
    min_size=0,
    max_size=10000,
)


class TestBoundedNumericScores:
    """Property 4: Bounded numeric scores."""

    @given(
        data_points=baseline_data_points_strategy,
        window=baseline_window_strategy,
    )
    @settings(max_examples=500)
    def test_confidence_always_bounded_zero_to_one(
        self, data_points: list[float], window: str
    ) -> None:
        """For any baseline data and window, confidence is in [0.0, 1.0].

        **Validates: Requirements 2.6, 4.5, 6.3**
        """
        confidence = compute_confidence(data_points, window)
        assert 0.0 <= confidence <= 1.0, (
            f"Confidence {confidence} out of bounds for "
            f"{len(data_points)} data points with window={window}"
        )

    @given(
        data_points=st.lists(
            st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False),
            min_size=1,
            max_size=287,
        ),
        window=baseline_window_strategy,
    )
    @settings(max_examples=300)
    def test_sparse_baseline_confidence_capped_at_half(
        self, data_points: list[float], window: str
    ) -> None:
        """When data points < 288 (sparse baseline), confidence <= 0.5.

        **Validates: Requirement 2.8**
        """
        assume(len(data_points) < 288)
        confidence = compute_confidence(data_points, window)
        assert confidence <= 0.5, (
            f"Sparse baseline confidence {confidence} exceeds 0.5 cap for "
            f"{len(data_points)} data points (< 288 threshold)"
        )

    @given(
        data_points=st.lists(
            st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False),
            min_size=288,
            max_size=10000,
        ),
        window=baseline_window_strategy,
    )
    @settings(max_examples=300)
    def test_non_sparse_baseline_confidence_above_half(
        self, data_points: list[float], window: str
    ) -> None:
        """When data points >= 288 (non-sparse), confidence > 0.5.

        **Validates: Requirements 2.6, 2.8**
        """
        confidence = compute_confidence(data_points, window)
        assert confidence > 0.5, (
            f"Non-sparse baseline confidence {confidence} not above 0.5 for "
            f"{len(data_points)} data points (>= 288 threshold)"
        )

    def test_empty_baseline_returns_zero_confidence(self) -> None:
        """Edge case: empty baseline always gives 0.0 confidence.

        **Validates: Requirements 2.6, 4.5, 6.3**
        """
        assert compute_confidence([], "7d") == 0.0
        assert compute_confidence([], "30d") == 0.0
