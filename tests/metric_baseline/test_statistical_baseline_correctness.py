"""Property-based test: Statistical baseline calculation correctness.

**Validates: Requirements 2.2, 2.3**

For any valid time series, verify mean/p95/p99/stddev match mathematical definitions:
1. mean equals sum(values) / len(values)
2. stddev equals sqrt(sum((x - mean)^2) / n) (population stddev)
3. p95 is between min and max of values
4. p99 >= p95
5. p95 >= median (p50) for any non-trivial list
"""

from __future__ import annotations

import math
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

from metric_baseline import compute_mean, compute_percentile, compute_stddev


# Strategy: non-empty lists of finite floats (no NaN, no inf) representing valid metric values
valid_time_series = st.lists(
    st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False),
    min_size=1,
    max_size=1000,
)

# Strategy: lists with at least 2 elements for percentile ordering tests
multi_value_time_series = st.lists(
    st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False),
    min_size=2,
    max_size=1000,
)


class TestStatisticalBaselineCorrectness:
    """Property 5: Statistical baseline calculation correctness."""

    @given(values=valid_time_series)
    @settings(max_examples=500)
    def test_mean_equals_sum_divided_by_length(self, values: list[float]) -> None:
        """Mean equals sum(values) / len(values) for any non-empty time series.

        **Validates: Requirements 2.2, 2.3**
        """
        result = compute_mean(values)
        expected = sum(values) / len(values)
        assert math.isclose(result, expected, rel_tol=1e-9, abs_tol=1e-12), (
            f"Mean mismatch: compute_mean={result}, expected={expected} "
            f"for {len(values)} values"
        )

    @given(values=valid_time_series)
    @settings(max_examples=500)
    def test_stddev_matches_population_formula(self, values: list[float]) -> None:
        """Stddev equals sqrt(sum((x - mean)^2) / n) (population stddev).

        **Validates: Requirements 2.2, 2.3**
        """
        mean = compute_mean(values)
        result = compute_stddev(values, mean)

        # Compute expected population stddev
        n = len(values)
        variance = sum((x - mean) ** 2 for x in values) / n
        expected = math.sqrt(variance)

        assert math.isclose(result, expected, rel_tol=1e-9, abs_tol=1e-12), (
            f"Stddev mismatch: compute_stddev={result}, expected={expected} "
            f"for {n} values with mean={mean}"
        )

    @given(values=valid_time_series)
    @settings(max_examples=500)
    def test_p95_bounded_by_min_and_max(self, values: list[float]) -> None:
        """P95 is between min and max of values for any non-empty time series.

        **Validates: Requirements 2.2, 2.3**
        """
        p95 = compute_percentile(values, 95.0)
        min_val = min(values)
        max_val = max(values)
        assert min_val <= p95 <= max_val, (
            f"P95={p95} not in [{min_val}, {max_val}] "
            f"for {len(values)} values"
        )

    @given(values=multi_value_time_series)
    @settings(max_examples=500)
    def test_p99_greater_than_or_equal_to_p95(self, values: list[float]) -> None:
        """P99 >= P95 for any time series with at least 2 values.

        **Validates: Requirements 2.2, 2.3**
        """
        p95 = compute_percentile(values, 95.0)
        p99 = compute_percentile(values, 99.0)
        assert p99 >= p95, (
            f"P99={p99} < P95={p95} violates percentile ordering "
            f"for {len(values)} values"
        )

    @given(values=multi_value_time_series)
    @settings(max_examples=500)
    def test_p95_greater_than_or_equal_to_median(self, values: list[float]) -> None:
        """P95 >= median (P50) for any time series with at least 2 values.

        **Validates: Requirements 2.2, 2.3**
        """
        p50 = compute_percentile(values, 50.0)
        p95 = compute_percentile(values, 95.0)
        assert p95 >= p50, (
            f"P95={p95} < P50={p50} violates percentile ordering "
            f"for {len(values)} values"
        )

    @given(values=valid_time_series)
    @settings(max_examples=300)
    def test_stddev_is_non_negative(self, values: list[float]) -> None:
        """Standard deviation is always non-negative.

        **Validates: Requirements 2.2, 2.3**
        """
        mean = compute_mean(values)
        stddev = compute_stddev(values, mean)
        assert stddev >= 0.0, (
            f"Stddev={stddev} is negative for {len(values)} values"
        )

    @given(values=valid_time_series)
    @settings(max_examples=300)
    def test_p99_bounded_by_min_and_max(self, values: list[float]) -> None:
        """P99 is between min and max of values for any non-empty time series.

        **Validates: Requirements 2.2, 2.3**
        """
        p99 = compute_percentile(values, 99.0)
        min_val = min(values)
        max_val = max(values)
        assert min_val <= p99 <= max_val, (
            f"P99={p99} not in [{min_val}, {max_val}] "
            f"for {len(values)} values"
        )
