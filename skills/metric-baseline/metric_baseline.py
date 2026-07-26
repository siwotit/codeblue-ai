"""Metric Baseline Comparison skill for CodeBlue AI.

Compares current metric values against historical baselines (7-day or 30-day)
to quantify whether observed values represent genuine anomalies or normal variance.

Input: BaselineRequest JSON on stdin
Output: SkillResponse wrapping MetricDeviation JSON on stdout

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

# Add parent paths for imports when running via uv
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from shared.mcp_client import cloudwatch_mcp
from shared.models import (
    DataPoint,
    DeviationClassification,
    MetricDeviation,
    SkillResponse,
    TimeRange,
)


# --- Statistical helpers ---


def compute_mean(values: list[float]) -> float:
    """Compute arithmetic mean of a list of values.

    Args:
        values: List of numeric values.

    Returns:
        The arithmetic mean, or 0.0 if empty.
    """
    if not values:
        return 0.0
    return sum(values) / len(values)


def compute_stddev(values: list[float], mean: float) -> float:
    """Compute population standard deviation.

    Uses population stddev (dividing by N, not N-1) as specified in the design.

    Args:
        values: List of numeric values.
        mean: Pre-computed mean of the values.

    Returns:
        Population standard deviation, or 0.0 if empty.
    """
    if not values:
        return 0.0
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return math.sqrt(variance)


def compute_percentile(values: list[float], percentile: float) -> float:
    """Compute the given percentile using sorted interpolation.

    Args:
        values: List of numeric values.
        percentile: Percentile to compute (0-100).

    Returns:
        The percentile value, or 0.0 if empty.
    """
    if not values:
        return 0.0
    sorted_values = sorted(values)
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]

    # Use linear interpolation between closest ranks
    rank = (percentile / 100.0) * (n - 1)
    lower_idx = int(math.floor(rank))
    upper_idx = int(math.ceil(rank))
    if lower_idx == upper_idx:
        return sorted_values[lower_idx]

    fraction = rank - lower_idx
    return sorted_values[lower_idx] + fraction * (
        sorted_values[upper_idx] - sorted_values[lower_idx]
    )


# --- Deviation classification ---

# Classification thresholds (in standard deviations)
NORMAL_THRESHOLD = 2.0
ELEVATED_THRESHOLD = 3.0
ANOMALOUS_THRESHOLD = 5.0

# Severity ordering for monotonic enforcement
_SEVERITY_ORDER = {
    DeviationClassification.NORMAL: 0,
    DeviationClassification.ELEVATED: 1,
    DeviationClassification.ANOMALOUS: 2,
    DeviationClassification.CRITICAL: 3,
}


def classify_deviation(deviation_factor: float) -> DeviationClassification:
    """Classify metric deviation based on standard deviations from baseline.

    Classification rules (Requirement 2.4):
    - normal: < 2σ
    - elevated: ≥ 2σ, < 3σ
    - anomalous: ≥ 3σ, < 5σ
    - critical: ≥ 5σ

    Args:
        deviation_factor: Absolute number of standard deviations from baseline mean.

    Returns:
        DeviationClassification enum value.
    """
    abs_deviation = abs(deviation_factor)
    if abs_deviation >= ANOMALOUS_THRESHOLD:
        return DeviationClassification.CRITICAL
    elif abs_deviation >= ELEVATED_THRESHOLD:
        return DeviationClassification.ANOMALOUS
    elif abs_deviation >= NORMAL_THRESHOLD:
        return DeviationClassification.ELEVATED
    else:
        return DeviationClassification.NORMAL


def enforce_monotonic_severity(
    new_classification: DeviationClassification,
    previous_classification: DeviationClassification | None,
) -> DeviationClassification:
    """Enforce monotonic severity: higher deviation never decreases classification.

    Requirement 2.5: When the deviation factor increases, the classification
    SHALL NOT decrease.

    Args:
        new_classification: The classification based on current deviation factor.
        previous_classification: The previous classification (if any).

    Returns:
        The classification that is at least as severe as the previous one.
    """
    if previous_classification is None:
        return new_classification
    if _SEVERITY_ORDER[new_classification] >= _SEVERITY_ORDER[previous_classification]:
        return new_classification
    return previous_classification


# --- Confidence computation ---


def compute_confidence(
    baseline_data_points: list[float],
    baseline_window: str,
) -> float:
    """Compute confidence value for the deviation assessment.

    Confidence is based on data density in the baseline period.
    Requirement 2.8: Capped at 0.5 for sparse baselines (<24h data).

    Assumes 5-minute metric resolution (288 points per day for CloudWatch).

    Args:
        baseline_data_points: The values collected from the baseline period.
        baseline_window: Either "7d" or "30d".

    Returns:
        Confidence value between 0.0 and 1.0.
    """
    n = len(baseline_data_points)

    if n == 0:
        return 0.0

    # CloudWatch typically has 5-min resolution = 288 points/day
    points_per_day = 288

    # 24 hours of data = 288 points at 5-min resolution
    sparse_threshold = points_per_day  # 288 points = 24h

    # Cap at 0.5 for sparse baselines (Requirement 2.8)
    if n < sparse_threshold:
        # Scale linearly from 0.1 to 0.5 based on available data
        confidence = 0.1 + 0.4 * (n / sparse_threshold)
        return min(confidence, 0.5)

    # For non-sparse baselines, scale from 0.6 to 1.0 based on density
    # Minimum adequate data: 1 day (288 points)
    # Excellent data: full window coverage
    if baseline_window == "30d":
        expected_points = 30 * points_per_day
    else:
        expected_points = 7 * points_per_day

    # Density above sparse threshold scales from 0.6 to 1.0
    density_above_sparse = min((n - sparse_threshold) / (expected_points - sparse_threshold), 1.0)
    confidence = 0.6 + (0.4 * density_above_sparse)

    # Clamp to valid range
    return max(0.0, min(1.0, confidence))


# --- Anomaly start time detection ---


def find_anomaly_start_time(
    data_points: list[DataPoint],
    baseline_mean: float,
    baseline_stddev: float,
) -> datetime | None:
    """Find the earliest point in the current window exceeding 2σ threshold.

    Requirement 2.7: Anomaly start time = earliest datapoint within the current
    1-hour window where the metric first exceeded the elevated threshold
    (≥ 2 standard deviations from baseline mean).

    Args:
        data_points: Current window data points (sorted by timestamp).
        baseline_mean: Mean of the baseline period.
        baseline_stddev: Standard deviation of the baseline period.

    Returns:
        ISO 8601 datetime of the earliest anomalous point, or None if no anomaly.
    """
    if not data_points or baseline_stddev == 0.0:
        return None

    threshold = baseline_mean + (NORMAL_THRESHOLD * baseline_stddev)

    # Sort by timestamp to find the earliest exceedance
    sorted_points = sorted(data_points, key=lambda dp: dp.timestamp)

    for point in sorted_points:
        if abs(point.value - baseline_mean) >= NORMAL_THRESHOLD * baseline_stddev:
            return point.timestamp

    return None


# --- MCP data retrieval ---


def fetch_metric_data(
    metric_name: str,
    namespace: str,
    dimensions: dict[str, str],
    start_time: datetime,
    end_time: datetime,
) -> list[DataPoint]:
    """Fetch metric data from CloudWatch via MCP.

    Args:
        metric_name: CloudWatch metric name.
        namespace: CloudWatch namespace.
        dimensions: Key-value dimension pairs.
        start_time: Start of the query window.
        end_time: End of the query window.

    Returns:
        List of DataPoint objects.

    Raises:
        RuntimeError: If the MCP invocation fails.
    """
    # Format dimensions for CloudWatch API
    dimension_filters = [
        {"Name": k, "Value": v} for k, v in dimensions.items()
    ]

    response = cloudwatch_mcp.invoke(
        "getMetricData",
        {
            "metricName": metric_name,
            "namespace": namespace,
            "dimensions": dimension_filters,
            "startTime": start_time.isoformat(),
            "endTime": end_time.isoformat(),
            "period": 300,  # 5-minute resolution
            "stat": "Average",
        },
    )

    if not response.success:
        raise RuntimeError(
            f"Metric data retrieval failed: {response.error}"
        )

    # Parse response data into DataPoint objects
    data_points: list[DataPoint] = []
    raw_points = response.data.get("dataPoints", [])
    for point in raw_points:
        ts = point.get("timestamp")
        val = point.get("value")
        if ts is not None and val is not None:
            if isinstance(ts, str):
                timestamp = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            else:
                timestamp = datetime.fromtimestamp(ts, tz=timezone.utc)
            data_points.append(DataPoint(timestamp=timestamp, value=float(val)))

    return data_points


# --- Main skill logic ---


def run_baseline_comparison(request: dict[str, Any]) -> dict[str, Any]:
    """Execute the metric baseline comparison.

    Args:
        request: BaselineRequest dictionary with keys:
            - metricName (str): CloudWatch metric name
            - namespace (str): CloudWatch namespace
            - dimensions (dict): Resource dimension key-value pairs
            - currentWindow (dict): {start, end} ISO 8601 timestamps
            - baselineWindow (str): "7d" or "30d"

    Returns:
        SkillResponse dictionary with status, message, and data fields.
    """
    # Validate required fields
    required_fields = ["metricName", "namespace", "dimensions", "currentWindow", "baselineWindow"]
    missing = [f for f in required_fields if f not in request]
    if missing:
        return SkillResponse(
            status="error",
            message=f"Missing required fields: {', '.join(missing)}",
            data=None,
        ).model_dump(by_alias=True)

    metric_name = request["metricName"]
    namespace = request["namespace"]
    dimensions = request["dimensions"]
    baseline_window = request["baselineWindow"]

    # Parse current window
    current_window = request["currentWindow"]
    try:
        current_start = datetime.fromisoformat(
            current_window["start"].replace("Z", "+00:00")
        )
        current_end = datetime.fromisoformat(
            current_window["end"].replace("Z", "+00:00")
        )
    except (KeyError, ValueError, TypeError) as e:
        return SkillResponse(
            status="error",
            message=f"Invalid currentWindow: {e}",
            data=None,
        ).model_dump(by_alias=True)

    # Determine baseline window
    if baseline_window == "30d":
        baseline_days = 30
    else:
        baseline_days = 7

    baseline_start = current_end - timedelta(days=baseline_days)
    # Baseline ends where current window starts to avoid overlap
    baseline_end = current_start

    # Fetch current window metric data (Requirement 2.1)
    try:
        current_data_points = fetch_metric_data(
            metric_name, namespace, dimensions, current_start, current_end
        )
    except RuntimeError as e:
        # Requirement 2.9: Handle metric retrieval failures gracefully
        return SkillResponse(
            status="error",
            message=str(e),
            data=None,
        ).model_dump(by_alias=True)

    # Fetch baseline metric data (Requirement 2.2, 2.3)
    try:
        baseline_data_points = fetch_metric_data(
            metric_name, namespace, dimensions, baseline_start, baseline_end
        )
    except RuntimeError as e:
        # Requirement 2.9: Handle metric retrieval failures gracefully
        return SkillResponse(
            status="error",
            message=str(e),
            data=None,
        ).model_dump(by_alias=True)

    # Extract values for statistical computation
    current_values = [dp.value for dp in current_data_points]
    baseline_values = [dp.value for dp in baseline_data_points]

    # Compute current window statistics
    current_mean = compute_mean(current_values)
    current_p95 = compute_percentile(current_values, 95.0)
    current_max = max(current_values) if current_values else 0.0

    # Compute baseline statistics (Requirement 2.2)
    baseline_mean = compute_mean(baseline_values)
    baseline_stddev = compute_stddev(baseline_values, baseline_mean)
    baseline_p95 = compute_percentile(baseline_values, 95.0)
    baseline_p99 = compute_percentile(baseline_values, 99.0)

    # Compute deviation factor
    # deviation_factor = (currentMean - baselineMean) / baselineStdDev
    if baseline_stddev > 0:
        deviation_factor = (current_mean - baseline_mean) / baseline_stddev
    else:
        # If stddev is 0 (all baseline values identical), use a simple approach
        if current_mean == baseline_mean:
            deviation_factor = 0.0
        elif baseline_mean != 0:
            # Use relative difference as a proxy
            deviation_factor = abs(current_mean - baseline_mean) / max(abs(baseline_mean), 1e-10)
        else:
            deviation_factor = 0.0 if current_mean == 0 else float("inf")

    # Classify deviation (Requirement 2.4)
    classification = classify_deviation(deviation_factor)

    # Compute confidence (Requirement 2.6, 2.8)
    confidence = compute_confidence(baseline_values, baseline_window)

    # Find anomaly start time (Requirement 2.7)
    anomaly_start_time = None
    if classification in (DeviationClassification.ANOMALOUS, DeviationClassification.CRITICAL):
        anomaly_start_time = find_anomaly_start_time(
            current_data_points, baseline_mean, baseline_stddev
        )

    # Build the time range for the response
    time_range = TimeRange(start=current_start, end=current_end)

    # Build MetricDeviation result
    metric_deviation = MetricDeviation(
        metricName=metric_name,
        namespace=namespace,
        dimensions=dimensions,
        timeRange=time_range,
        currentMean=round(current_mean, 4),
        currentP95=round(current_p95, 4),
        currentMax=round(current_max, 4),
        baselineMean=round(baseline_mean, 4),
        baselineP95=round(baseline_p95, 4),
        baselineP99=round(baseline_p99, 4),
        baselineStdDev=round(baseline_stddev, 4),
        deviationFactor=round(deviation_factor, 4),
        classification=classification,
        confidence=round(confidence, 4),
        anomalyStartTime=anomaly_start_time,
        dataPoints=current_data_points,
        baselineDataPoints=baseline_data_points,
    )

    # Build success response
    response = SkillResponse(
        status="success",
        message=f"Baseline comparison complete for {metric_name}",
        data=json.loads(metric_deviation.model_dump_json(by_alias=True)),
    )

    return response.model_dump(by_alias=True)


def main() -> None:
    """Entry point: read BaselineRequest from stdin, write SkillResponse to stdout."""
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            result = SkillResponse(
                status="error",
                message="No input provided on stdin",
                data=None,
            ).model_dump(by_alias=True)
        else:
            request = json.loads(raw_input)
            result = run_baseline_comparison(request)
    except json.JSONDecodeError as e:
        result = SkillResponse(
            status="error",
            message=f"Invalid JSON input: {e}",
            data=None,
        ).model_dump(by_alias=True)
    except Exception as e:
        result = SkillResponse(
            status="error",
            message=f"Unexpected error: {type(e).__name__}: {e}",
            data=None,
        ).model_dump(by_alias=True)

    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
