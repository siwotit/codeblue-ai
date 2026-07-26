"""Unit tests for the metric_baseline skill.

Tests the statistical computation, deviation classification,
confidence scoring, and error handling logic.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "skills"))
sys.path.insert(
    0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "skills" / "metric-baseline")
)

from metric_baseline import (
    classify_deviation,
    compute_confidence,
    compute_mean,
    compute_percentile,
    compute_stddev,
    enforce_monotonic_severity,
    find_anomaly_start_time,
    run_baseline_comparison,
)
from shared.models import DataPoint, DeviationClassification


# --- compute_mean tests ---


class TestComputeMean:
    def test_basic_mean(self) -> None:
        assert compute_mean([1, 2, 3, 4, 5]) == 3.0

    def test_single_value(self) -> None:
        assert compute_mean([42.0]) == 42.0

    def test_empty_list(self) -> None:
        assert compute_mean([]) == 0.0

    def test_negative_values(self) -> None:
        assert compute_mean([-1, -2, -3]) == -2.0

    def test_mixed_values(self) -> None:
        assert compute_mean([-10, 10]) == 0.0


# --- compute_stddev tests ---


class TestComputeStddev:
    def test_known_stddev(self) -> None:
        """Population stddev of [2,4,4,4,5,5,7,9] is 2.0."""
        values = [2, 4, 4, 4, 5, 5, 7, 9]
        mean = compute_mean(values)
        stddev = compute_stddev(values, mean)
        assert abs(stddev - 2.0) < 0.01

    def test_zero_stddev(self) -> None:
        """All identical values have stddev 0."""
        values = [5.0, 5.0, 5.0, 5.0]
        stddev = compute_stddev(values, 5.0)
        assert stddev == 0.0

    def test_empty_list(self) -> None:
        assert compute_stddev([], 0.0) == 0.0

    def test_single_value(self) -> None:
        assert compute_stddev([10.0], 10.0) == 0.0


# --- compute_percentile tests ---


class TestComputePercentile:
    def test_p95_hundred_values(self) -> None:
        values = list(range(1, 101))
        p95 = compute_percentile(values, 95.0)
        assert abs(p95 - 95.05) < 0.5

    def test_p99_hundred_values(self) -> None:
        values = list(range(1, 101))
        p99 = compute_percentile(values, 99.0)
        assert abs(p99 - 99.01) < 0.5

    def test_p50_is_median(self) -> None:
        values = [1, 2, 3, 4, 5]
        p50 = compute_percentile(values, 50.0)
        assert p50 == 3.0

    def test_empty_list(self) -> None:
        assert compute_percentile([], 95.0) == 0.0

    def test_single_value(self) -> None:
        assert compute_percentile([42.0], 95.0) == 42.0

    def test_p0_returns_minimum(self) -> None:
        values = [5, 10, 15, 20]
        assert compute_percentile(values, 0.0) == 5.0

    def test_p100_returns_maximum(self) -> None:
        values = [5, 10, 15, 20]
        assert compute_percentile(values, 100.0) == 20.0


# --- classify_deviation tests ---


class TestClassifyDeviation:
    def test_normal_below_2sigma(self) -> None:
        assert classify_deviation(0.0) == DeviationClassification.NORMAL
        assert classify_deviation(1.0) == DeviationClassification.NORMAL
        assert classify_deviation(1.99) == DeviationClassification.NORMAL

    def test_elevated_2_to_3_sigma(self) -> None:
        assert classify_deviation(2.0) == DeviationClassification.ELEVATED
        assert classify_deviation(2.5) == DeviationClassification.ELEVATED
        assert classify_deviation(2.99) == DeviationClassification.ELEVATED

    def test_anomalous_3_to_5_sigma(self) -> None:
        assert classify_deviation(3.0) == DeviationClassification.ANOMALOUS
        assert classify_deviation(4.0) == DeviationClassification.ANOMALOUS
        assert classify_deviation(4.99) == DeviationClassification.ANOMALOUS

    def test_critical_5_plus_sigma(self) -> None:
        assert classify_deviation(5.0) == DeviationClassification.CRITICAL
        assert classify_deviation(10.0) == DeviationClassification.CRITICAL
        assert classify_deviation(100.0) == DeviationClassification.CRITICAL

    def test_negative_deviation_uses_absolute_value(self) -> None:
        assert classify_deviation(-2.5) == DeviationClassification.ELEVATED
        assert classify_deviation(-3.5) == DeviationClassification.ANOMALOUS
        assert classify_deviation(-5.0) == DeviationClassification.CRITICAL


# --- enforce_monotonic_severity tests ---


class TestEnforceMonotonicSeverity:
    def test_no_previous(self) -> None:
        assert (
            enforce_monotonic_severity(DeviationClassification.NORMAL, None)
            == DeviationClassification.NORMAL
        )

    def test_cannot_decrease(self) -> None:
        assert (
            enforce_monotonic_severity(
                DeviationClassification.NORMAL, DeviationClassification.ELEVATED
            )
            == DeviationClassification.ELEVATED
        )

    def test_can_increase(self) -> None:
        assert (
            enforce_monotonic_severity(
                DeviationClassification.CRITICAL, DeviationClassification.ELEVATED
            )
            == DeviationClassification.CRITICAL
        )

    def test_same_stays_same(self) -> None:
        assert (
            enforce_monotonic_severity(
                DeviationClassification.ANOMALOUS, DeviationClassification.ANOMALOUS
            )
            == DeviationClassification.ANOMALOUS
        )


# --- compute_confidence tests ---


class TestComputeConfidence:
    def test_empty_returns_zero(self) -> None:
        assert compute_confidence([], "7d") == 0.0

    def test_sparse_capped_at_half(self) -> None:
        """Fewer than 288 points (24h) should cap confidence at 0.5."""
        conf = compute_confidence([1.0] * 100, "7d")
        assert conf <= 0.5
        assert conf > 0.0

    def test_sparse_boundary(self) -> None:
        """Exactly at sparse threshold boundary should be >= 0.5."""
        # 287 points is sparse (< 288)
        conf_sparse = compute_confidence([1.0] * 287, "7d")
        assert conf_sparse <= 0.5

    def test_adequate_data_above_half(self) -> None:
        """More than 288 points (>24h) should give confidence > 0.5."""
        conf = compute_confidence([1.0] * 500, "7d")
        assert conf > 0.5

    def test_full_coverage_high_confidence(self) -> None:
        """Full 7-day data should give high confidence."""
        conf = compute_confidence([1.0] * 2016, "7d")
        assert conf >= 0.95

    def test_30d_window_scales_correctly(self) -> None:
        """30-day window has higher expected count."""
        conf_7d = compute_confidence([1.0] * 1000, "7d")
        conf_30d = compute_confidence([1.0] * 1000, "30d")
        # Same number of points should give lower confidence for 30d window
        assert conf_30d < conf_7d

    def test_always_between_zero_and_one(self) -> None:
        for n in [0, 1, 10, 100, 288, 500, 2016, 10000]:
            conf = compute_confidence([1.0] * n, "7d")
            assert 0.0 <= conf <= 1.0


# --- find_anomaly_start_time tests ---


class TestFindAnomalyStartTime:
    def test_finds_first_exceedance(self) -> None:
        points = [
            DataPoint(
                timestamp=datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc), value=40.0
            ),
            DataPoint(
                timestamp=datetime(2024, 1, 15, 10, 15, tzinfo=timezone.utc), value=42.0
            ),
            DataPoint(
                timestamp=datetime(2024, 1, 15, 10, 30, tzinfo=timezone.utc), value=80.0
            ),
            DataPoint(
                timestamp=datetime(2024, 1, 15, 10, 45, tzinfo=timezone.utc), value=90.0
            ),
        ]
        result = find_anomaly_start_time(points, baseline_mean=42.5, baseline_stddev=8.7)
        assert result == datetime(2024, 1, 15, 10, 30, tzinfo=timezone.utc)

    def test_no_exceedance_returns_none(self) -> None:
        points = [
            DataPoint(
                timestamp=datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc), value=40.0
            ),
            DataPoint(
                timestamp=datetime(2024, 1, 15, 10, 15, tzinfo=timezone.utc), value=45.0
            ),
        ]
        # 45 - 42.5 = 2.5, threshold is 2*8.7 = 17.4. 2.5 < 17.4
        result = find_anomaly_start_time(points, baseline_mean=42.5, baseline_stddev=8.7)
        assert result is None

    def test_empty_points(self) -> None:
        result = find_anomaly_start_time([], baseline_mean=42.5, baseline_stddev=8.7)
        assert result is None

    def test_zero_stddev(self) -> None:
        points = [
            DataPoint(
                timestamp=datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc), value=50.0
            ),
        ]
        result = find_anomaly_start_time(points, baseline_mean=42.5, baseline_stddev=0.0)
        assert result is None

    def test_unsorted_points_finds_earliest(self) -> None:
        """Points should be sorted by timestamp to find earliest exceedance."""
        points = [
            DataPoint(
                timestamp=datetime(2024, 1, 15, 10, 45, tzinfo=timezone.utc), value=90.0
            ),
            DataPoint(
                timestamp=datetime(2024, 1, 15, 10, 15, tzinfo=timezone.utc), value=80.0
            ),
            DataPoint(
                timestamp=datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc), value=40.0
            ),
        ]
        result = find_anomaly_start_time(points, baseline_mean=42.5, baseline_stddev=8.7)
        # 80 - 42.5 = 37.5, threshold is 17.4. First exceedance is 10:15
        assert result == datetime(2024, 1, 15, 10, 15, tzinfo=timezone.utc)


# --- run_baseline_comparison tests ---


class TestRunBaselineComparison:
    def test_missing_fields_returns_error(self) -> None:
        result = run_baseline_comparison({})
        assert result["status"] == "error"
        assert "Missing required fields" in result["message"]

    def test_invalid_window_returns_error(self) -> None:
        result = run_baseline_comparison(
            {
                "metricName": "CPUUtilization",
                "namespace": "AWS/ECS",
                "dimensions": {"ClusterName": "prod"},
                "currentWindow": {"start": "bad", "end": "bad"},
                "baselineWindow": "7d",
            }
        )
        assert result["status"] == "error"
        assert "Invalid currentWindow" in result["message"]

    def test_mcp_failure_returns_error(self) -> None:
        """MCP unavailability should produce a graceful error response."""
        result = run_baseline_comparison(
            {
                "metricName": "CPUUtilization",
                "namespace": "AWS/ECS",
                "dimensions": {"ClusterName": "prod"},
                "currentWindow": {
                    "start": "2024-01-15T10:00:00Z",
                    "end": "2024-01-15T11:00:00Z",
                },
                "baselineWindow": "7d",
            }
        )
        assert result["status"] == "error"
        assert "retrieval failed" in result["message"]

    @patch("metric_baseline.fetch_metric_data")
    def test_successful_comparison(self, mock_fetch) -> None:
        """Full baseline comparison with mocked metric data."""
        # Setup: current window shows high CPU
        current_points = [
            DataPoint(
                timestamp=datetime(2024, 1, 15, 10, i * 5, tzinfo=timezone.utc),
                value=85.0 + i,
            )
            for i in range(12)  # 12 points over 1h
        ]

        # Setup: baseline shows normal CPU (~42 mean, ~8.7 stddev)
        import random

        random.seed(42)
        baseline_points = [
            DataPoint(
                timestamp=datetime(2024, 1, 8, 0, 0, tzinfo=timezone.utc)
                + timedelta(minutes=5 * i),
                value=42.5 + random.gauss(0, 8.7),
            )
            for i in range(2016)  # 7 days
        ]

        mock_fetch.side_effect = [current_points, baseline_points]

        result = run_baseline_comparison(
            {
                "metricName": "CPUUtilization",
                "namespace": "AWS/ECS",
                "dimensions": {"ClusterName": "prod", "ServiceName": "payments"},
                "currentWindow": {
                    "start": "2024-01-15T10:00:00Z",
                    "end": "2024-01-15T11:00:00Z",
                },
                "baselineWindow": "7d",
            }
        )

        assert result["status"] == "success"
        assert result["data"] is not None

        data = result["data"]
        assert data["metricName"] == "CPUUtilization"
        assert data["namespace"] == "AWS/ECS"
        assert data["classification"] in ("anomalous", "critical")
        assert 0.0 <= data["confidence"] <= 1.0
        assert data["deviationFactor"] > 3.0  # Should be well above baseline
        assert data["currentMean"] > 80.0
        assert data["baselineStdDev"] > 0

    @patch("metric_baseline.fetch_metric_data")
    def test_normal_values_classified_normal(self, mock_fetch) -> None:
        """Values within 2 sigma are classified as normal."""
        current_points = [
            DataPoint(
                timestamp=datetime(2024, 1, 15, 10, i * 5, tzinfo=timezone.utc),
                value=43.0,
            )
            for i in range(12)
        ]
        baseline_points = [
            DataPoint(
                timestamp=datetime(2024, 1, 8, 0, 0, tzinfo=timezone.utc)
                + timedelta(minutes=5 * i),
                value=42.5,
            )
            for i in range(2016)
        ]
        # All baseline values are identical (stddev=0), so let's add variance
        import random

        random.seed(123)
        for i in range(len(baseline_points)):
            baseline_points[i] = DataPoint(
                timestamp=baseline_points[i].timestamp,
                value=42.5 + random.gauss(0, 5.0),
            )

        mock_fetch.side_effect = [current_points, baseline_points]

        result = run_baseline_comparison(
            {
                "metricName": "CPUUtilization",
                "namespace": "AWS/ECS",
                "dimensions": {"ClusterName": "prod"},
                "currentWindow": {
                    "start": "2024-01-15T10:00:00Z",
                    "end": "2024-01-15T11:00:00Z",
                },
                "baselineWindow": "7d",
            }
        )

        assert result["status"] == "success"
        data = result["data"]
        assert data["classification"] == "normal"
        assert data["anomalyStartTime"] is None

    @patch("metric_baseline.fetch_metric_data")
    def test_sparse_baseline_caps_confidence(self, mock_fetch) -> None:
        """Sparse baseline (<24h data) caps confidence at 0.5."""
        current_points = [
            DataPoint(
                timestamp=datetime(2024, 1, 15, 10, i * 5, tzinfo=timezone.utc),
                value=90.0,
            )
            for i in range(12)
        ]
        # Only 50 baseline points (less than 288 = 24h)
        baseline_points = [
            DataPoint(
                timestamp=datetime(2024, 1, 14, 0, 0, tzinfo=timezone.utc)
                + timedelta(minutes=5 * i),
                value=42.5 + (i % 10),
            )
            for i in range(50)
        ]

        mock_fetch.side_effect = [current_points, baseline_points]

        result = run_baseline_comparison(
            {
                "metricName": "CPUUtilization",
                "namespace": "AWS/ECS",
                "dimensions": {"ClusterName": "prod"},
                "currentWindow": {
                    "start": "2024-01-15T10:00:00Z",
                    "end": "2024-01-15T11:00:00Z",
                },
                "baselineWindow": "7d",
            }
        )

        assert result["status"] == "success"
        data = result["data"]
        assert data["confidence"] <= 0.5

    @patch("metric_baseline.fetch_metric_data")
    def test_30d_baseline_window(self, mock_fetch) -> None:
        """30-day baseline is supported."""
        current_points = [
            DataPoint(
                timestamp=datetime(2024, 1, 15, 10, i * 5, tzinfo=timezone.utc),
                value=90.0,
            )
            for i in range(12)
        ]
        baseline_points = [
            DataPoint(
                timestamp=datetime(2023, 12, 16, 0, 0, tzinfo=timezone.utc)
                + timedelta(minutes=5 * i),
                value=42.5,
            )
            for i in range(500)
        ]

        mock_fetch.side_effect = [current_points, baseline_points]

        result = run_baseline_comparison(
            {
                "metricName": "CPUUtilization",
                "namespace": "AWS/ECS",
                "dimensions": {"ClusterName": "prod"},
                "currentWindow": {
                    "start": "2024-01-15T10:00:00Z",
                    "end": "2024-01-15T11:00:00Z",
                },
                "baselineWindow": "30d",
            }
        )

        assert result["status"] == "success"
        data = result["data"]
        assert data["metricName"] == "CPUUtilization"
