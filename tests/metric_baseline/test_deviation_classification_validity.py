"""Property-based test: Deviation classification validity.

**Validates: Requirements 2.4, 2.7**

For any classification result, verify it is one of the four valid values
(normal, elevated, anomalous, critical) and anomaly_start_time is present
when the classification is anomalous or critical.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

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

from metric_baseline import classify_deviation, find_anomaly_start_time
from shared.models import DataPoint, DeviationClassification


# Valid classification values per Requirement 2.4
VALID_CLASSIFICATIONS = {
    DeviationClassification.NORMAL,
    DeviationClassification.ELEVATED,
    DeviationClassification.ANOMALOUS,
    DeviationClassification.CRITICAL,
}

# Strategy for arbitrary deviation factors (any real number)
deviation_factor_strategy = st.floats(
    min_value=-10000.0, max_value=10000.0, allow_nan=False, allow_infinity=False
)

# Strategy for positive baseline stats
positive_float = st.floats(min_value=0.1, max_value=1000.0, allow_nan=False, allow_infinity=False)

# Strategy for baseline mean (can be any value)
baseline_mean_strategy = st.floats(
    min_value=-1000.0, max_value=1000.0, allow_nan=False, allow_infinity=False
)

# Strategy for positive stddev
stddev_strategy = st.floats(
    min_value=0.1, max_value=100.0, allow_nan=False, allow_infinity=False
)


def make_data_point(value: float, minutes_ago: int) -> DataPoint:
    """Create a DataPoint at a given number of minutes before now."""
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return DataPoint(timestamp=ts, value=value)


class TestDeviationClassificationValidity:
    """Property 6: Deviation classification validity."""

    @given(deviation_factor=deviation_factor_strategy)
    @settings(max_examples=1000)
    def test_classification_is_one_of_four_valid_values(
        self, deviation_factor: float
    ) -> None:
        """For any deviation factor, classification must be one of the four valid values.

        **Validates: Requirement 2.4**
        """
        classification = classify_deviation(deviation_factor)

        assert classification in VALID_CLASSIFICATIONS, (
            f"classify_deviation({deviation_factor}) returned {classification}, "
            f"which is not one of the valid classifications: "
            f"{[c.value for c in VALID_CLASSIFICATIONS]}"
        )

    @given(
        baseline_mean=baseline_mean_strategy,
        baseline_stddev=stddev_strategy,
        num_points=st.integers(min_value=1, max_value=20),
        deviation_multiplier=st.floats(
            min_value=3.0, max_value=50.0, allow_nan=False, allow_infinity=False
        ),
    )
    @settings(max_examples=500)
    def test_anomaly_start_time_present_when_anomalous_or_critical(
        self,
        baseline_mean: float,
        baseline_stddev: float,
        num_points: int,
        deviation_multiplier: float,
    ) -> None:
        """When data points deviate >= 2σ from baseline, find_anomaly_start_time returns non-None.

        For anomalous/critical classifications, at least one data point exceeds
        the elevated threshold (2σ), so anomaly_start_time must be identified.

        **Validates: Requirement 2.7**
        """
        # Create data points that clearly exceed the 2σ threshold
        # Each point is at baseline_mean + deviation_multiplier * stddev
        anomalous_value = baseline_mean + (deviation_multiplier * baseline_stddev)
        data_points = [
            make_data_point(anomalous_value, minutes_ago=i)
            for i in range(num_points)
        ]

        # Verify the deviation factor would classify as anomalous or critical
        deviation_factor = (anomalous_value - baseline_mean) / baseline_stddev
        classification = classify_deviation(deviation_factor)
        assume(classification in (DeviationClassification.ANOMALOUS, DeviationClassification.CRITICAL))

        anomaly_start = find_anomaly_start_time(data_points, baseline_mean, baseline_stddev)

        assert anomaly_start is not None, (
            f"find_anomaly_start_time returned None for data points with "
            f"deviation_multiplier={deviation_multiplier}σ "
            f"(classification={classification.value}), "
            f"baseline_mean={baseline_mean}, baseline_stddev={baseline_stddev}"
        )

    @given(
        baseline_mean=baseline_mean_strategy,
        baseline_stddev=stddev_strategy,
        num_points=st.integers(min_value=1, max_value=20),
        offset_fraction=st.floats(
            min_value=0.0, max_value=0.9, allow_nan=False, allow_infinity=False
        ),
    )
    @settings(max_examples=500)
    def test_anomaly_start_time_none_when_within_normal_range(
        self,
        baseline_mean: float,
        baseline_stddev: float,
        num_points: int,
        offset_fraction: float,
    ) -> None:
        """When all data points are within < 2σ of baseline, find_anomaly_start_time returns None.

        **Validates: Requirement 2.7**
        """
        # Create data points within the normal range (< 2σ from mean)
        # offset_fraction ranges 0-0.9, so max offset is 0.9 * 2 * stddev = 1.8σ < 2σ
        normal_value = baseline_mean + (offset_fraction * baseline_stddev)
        data_points = [
            make_data_point(normal_value, minutes_ago=i)
            for i in range(num_points)
        ]

        anomaly_start = find_anomaly_start_time(data_points, baseline_mean, baseline_stddev)

        assert anomaly_start is None, (
            f"find_anomaly_start_time returned {anomaly_start} for data points "
            f"within normal range (offset={offset_fraction}σ < 2σ), "
            f"baseline_mean={baseline_mean}, baseline_stddev={baseline_stddev}, "
            f"value={normal_value}"
        )
