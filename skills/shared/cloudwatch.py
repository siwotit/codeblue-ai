"""Shared CloudWatch module for CodeBlue AI.

Provides metric queries, log queries, and baseline comparison logic.
This is a shared utility — not a skill. Service skills (eks-triage, ec2-triage, etc.)
call into this module for their CloudWatch needs.

Consolidates functionality from the former metric-baseline and log-triage skills.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from skills.shared.mcp_client import cloudwatch_mcp, kubernetes_mcp
from skills.shared.models import (
    DataPoint,
    DeviationClassification,
    LogFinding,
    LogFindings,
    LogSeverity,
    MetricDeviation,
    TimeRange,
)


# ============================================================================
# Metric Baseline Comparison
# ============================================================================

# Classification thresholds (in standard deviations)
NORMAL_THRESHOLD = 2.0
ELEVATED_THRESHOLD = 3.0
ANOMALOUS_THRESHOLD = 5.0

DEFAULT_ERROR_PATTERNS: list[str] = [
    "ERROR", "FATAL", "Exception", "Timeout", "ConnectionRefused",
]

MAX_SAMPLE_LINES = 5


# --- Statistical helpers ---


def compute_mean(values: list[float]) -> float:
    """Compute arithmetic mean."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def compute_stddev(values: list[float], mean: float) -> float:
    """Compute population standard deviation."""
    if not values:
        return 0.0
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return math.sqrt(variance)


def compute_percentile(values: list[float], percentile: float) -> float:
    """Compute the given percentile using sorted interpolation."""
    if not values:
        return 0.0
    sorted_values = sorted(values)
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    rank = (percentile / 100.0) * (n - 1)
    lower_idx = int(math.floor(rank))
    upper_idx = int(math.ceil(rank))
    if lower_idx == upper_idx:
        return sorted_values[lower_idx]
    fraction = rank - lower_idx
    return sorted_values[lower_idx] + fraction * (sorted_values[upper_idx] - sorted_values[lower_idx])


def classify_deviation(deviation_factor: float) -> DeviationClassification:
    """Classify metric deviation based on standard deviations from baseline."""
    abs_deviation = abs(deviation_factor)
    if abs_deviation >= ANOMALOUS_THRESHOLD:
        return DeviationClassification.CRITICAL
    elif abs_deviation >= ELEVATED_THRESHOLD:
        return DeviationClassification.ANOMALOUS
    elif abs_deviation >= NORMAL_THRESHOLD:
        return DeviationClassification.ELEVATED
    else:
        return DeviationClassification.NORMAL


def compute_confidence(baseline_data_points: list[float], baseline_window: str) -> float:
    """Compute confidence value based on baseline data density."""
    n = len(baseline_data_points)
    if n == 0:
        return 0.0
    points_per_day = 288
    sparse_threshold = points_per_day

    if n < sparse_threshold:
        confidence = 0.1 + 0.4 * (n / sparse_threshold)
        return min(confidence, 0.5)

    expected_points = (30 if baseline_window == "30d" else 7) * points_per_day
    density_above_sparse = min((n - sparse_threshold) / (expected_points - sparse_threshold), 1.0)
    confidence = 0.6 + (0.4 * density_above_sparse)
    return max(0.0, min(1.0, confidence))


def find_anomaly_start_time(
    data_points: list[DataPoint], baseline_mean: float, baseline_stddev: float
) -> datetime | None:
    """Find earliest point in the current window exceeding 2σ threshold."""
    if not data_points or baseline_stddev == 0.0:
        return None
    sorted_points = sorted(data_points, key=lambda dp: dp.timestamp)
    for point in sorted_points:
        if abs(point.value - baseline_mean) >= NORMAL_THRESHOLD * baseline_stddev:
            return point.timestamp
    return None


# --- Metric Data Fetching ---


def fetch_metric_data(
    metric_name: str, namespace: str, dimensions: dict[str, str],
    start_time: datetime, end_time: datetime,
) -> list[DataPoint]:
    """Fetch metric data from CloudWatch via MCP."""
    dimension_filters = [{"Name": k, "Value": v} for k, v in dimensions.items()]
    response = cloudwatch_mcp.invoke(
        "getMetricData",
        {
            "metricName": metric_name, "namespace": namespace,
            "dimensions": dimension_filters,
            "startTime": start_time.isoformat(), "endTime": end_time.isoformat(),
            "period": 300, "stat": "Average",
        },
    )
    if not response.success:
        raise RuntimeError(f"Metric data retrieval failed: {response.error}")

    data_points: list[DataPoint] = []
    for point in response.data.get("dataPoints", []):
        ts = point.get("timestamp")
        val = point.get("value")
        if ts is not None and val is not None:
            if isinstance(ts, str):
                timestamp = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            else:
                timestamp = datetime.fromtimestamp(ts, tz=timezone.utc)
            data_points.append(DataPoint(timestamp=timestamp, value=float(val)))
    return data_points


# --- Baseline Comparison (main public API) ---


def run_baseline_comparison(request: dict[str, Any]) -> dict[str, Any]:
    """Execute metric baseline comparison.

    Args:
        request: Dict with keys: metricName, namespace, dimensions, currentWindow, baselineWindow

    Returns:
        Dict with status, message, and data (MetricDeviation) fields.
    """
    from skills.shared.models import SkillResponse

    required_fields = ["metricName", "namespace", "dimensions", "currentWindow", "baselineWindow"]
    missing = [f for f in required_fields if f not in request]
    if missing:
        return SkillResponse(status="error", message=f"Missing required fields: {', '.join(missing)}", data=None).model_dump(by_alias=True)

    metric_name = request["metricName"]
    namespace = request["namespace"]
    dimensions = request["dimensions"]
    baseline_window = request["baselineWindow"]

    current_window = request["currentWindow"]
    try:
        current_start = datetime.fromisoformat(current_window["start"].replace("Z", "+00:00"))
        current_end = datetime.fromisoformat(current_window["end"].replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError) as e:
        return SkillResponse(status="error", message=f"Invalid currentWindow: {e}", data=None).model_dump(by_alias=True)

    baseline_days = 30 if baseline_window == "30d" else 7
    baseline_start = current_end - timedelta(days=baseline_days)
    baseline_end = current_start

    try:
        current_data_points = fetch_metric_data(metric_name, namespace, dimensions, current_start, current_end)
    except RuntimeError as e:
        return SkillResponse(status="error", message=str(e), data=None).model_dump(by_alias=True)

    try:
        baseline_data_points = fetch_metric_data(metric_name, namespace, dimensions, baseline_start, baseline_end)
    except RuntimeError as e:
        return SkillResponse(status="error", message=str(e), data=None).model_dump(by_alias=True)

    current_values = [dp.value for dp in current_data_points]
    baseline_values = [dp.value for dp in baseline_data_points]

    current_mean = compute_mean(current_values)
    current_p95 = compute_percentile(current_values, 95.0)
    current_max = max(current_values) if current_values else 0.0

    baseline_mean = compute_mean(baseline_values)
    baseline_stddev = compute_stddev(baseline_values, baseline_mean)
    baseline_p95 = compute_percentile(baseline_values, 95.0)
    baseline_p99 = compute_percentile(baseline_values, 99.0)

    if baseline_stddev > 0:
        deviation_factor = (current_mean - baseline_mean) / baseline_stddev
    else:
        if current_mean == baseline_mean:
            deviation_factor = 0.0
        elif baseline_mean != 0:
            deviation_factor = abs(current_mean - baseline_mean) / max(abs(baseline_mean), 1e-10)
        else:
            deviation_factor = 0.0 if current_mean == 0 else float("inf")

    classification = classify_deviation(deviation_factor)
    confidence = compute_confidence(baseline_values, baseline_window)

    anomaly_start_time = None
    if classification in (DeviationClassification.ANOMALOUS, DeviationClassification.CRITICAL):
        anomaly_start_time = find_anomaly_start_time(current_data_points, baseline_mean, baseline_stddev)

    time_range = TimeRange(start=current_start, end=current_end)
    metric_deviation = MetricDeviation(
        metricName=metric_name, namespace=namespace, dimensions=dimensions, timeRange=time_range,
        currentMean=round(current_mean, 4), currentP95=round(current_p95, 4), currentMax=round(current_max, 4),
        baselineMean=round(baseline_mean, 4), baselineP95=round(baseline_p95, 4),
        baselineP99=round(baseline_p99, 4), baselineStdDev=round(baseline_stddev, 4),
        deviationFactor=round(deviation_factor, 4), classification=classification,
        confidence=round(confidence, 4), anomalyStartTime=anomaly_start_time,
        dataPoints=current_data_points, baselineDataPoints=baseline_data_points,
    )

    return SkillResponse(
        status="success", message=f"Baseline comparison complete for {metric_name}",
        data=json.loads(metric_deviation.model_dump_json(by_alias=True)),
    ).model_dump(by_alias=True)


# ============================================================================
# Log Triage
# ============================================================================


def classify_pattern_severity(pattern: str) -> LogSeverity:
    """Classify a log pattern into a severity level."""
    pattern_lower = pattern.lower()
    if "fatal" in pattern_lower or "panic" in pattern_lower:
        return LogSeverity.FATAL
    elif "warn" in pattern_lower:
        return LogSeverity.WARNING
    return LogSeverity.ERROR


def query_log_group(
    log_group: str, start_time: datetime, end_time: datetime, patterns: list[str]
) -> dict[str, Any] | None:
    """Query a single CloudWatch log group for error patterns."""
    filter_clauses = " or ".join(f'@message like /{p}/' for p in patterns)
    query = f"fields @timestamp, @message, @logStream\n| filter ({filter_clauses})\n| sort @timestamp desc\n| limit 1000"

    response = cloudwatch_mcp.invoke(
        "queryLogInsights",
        {"logGroupName": log_group, "query": query, "startTime": start_time.isoformat(), "endTime": end_time.isoformat()},
    )
    if not response.success:
        return None
    return response.data


def query_baseline_count(
    log_group: str, start_time: datetime, end_time: datetime, patterns: list[str]
) -> int:
    """Query the baseline error count for a log group."""
    filter_clauses = " or ".join(f'@message like /{p}/' for p in patterns)
    query = f"fields @timestamp, @message\n| filter ({filter_clauses})\n| stats count(*) as errorCount"

    response = cloudwatch_mcp.invoke(
        "queryLogInsights",
        {"logGroupName": log_group, "query": query, "startTime": start_time.isoformat(), "endTime": end_time.isoformat()},
    )
    if not response.success:
        return 0
    results = response.data.get("results", [])
    if results:
        try:
            return int(results[0].get("errorCount", 0))
        except (ValueError, TypeError):
            return 0
    return 0


def run_log_triage(request: dict[str, Any]) -> dict[str, Any]:
    """Execute log triage analysis.

    Args:
        request: Dict with keys: logGroups, timeRange, namespaces (opt), errorPatterns (opt)

    Returns:
        SkillResponse dict.
    """
    from skills.shared.models import SkillResponse

    if "logGroups" not in request:
        return SkillResponse(status="error", message="Missing required field: logGroups", data=None).model_dump(by_alias=True)
    if "timeRange" not in request:
        return SkillResponse(status="error", message="Missing required field: timeRange", data=None).model_dump(by_alias=True)

    log_groups = request["logGroups"]
    if not log_groups or not isinstance(log_groups, list):
        return SkillResponse(status="error", message="logGroups must be a non-empty list", data=None).model_dump(by_alias=True)

    error_patterns = request.get("errorPatterns", DEFAULT_ERROR_PATTERNS)
    time_range_raw = request["timeRange"]

    try:
        current_start = datetime.fromisoformat(time_range_raw["start"].replace("Z", "+00:00"))
        current_end = datetime.fromisoformat(time_range_raw["end"].replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError) as e:
        return SkillResponse(status="error", message=f"Invalid timeRange: {e}", data=None).model_dump(by_alias=True)

    baseline_end = current_start
    baseline_start = baseline_end - timedelta(days=7)

    all_findings: list[dict[str, Any]] = []
    queried_log_groups: list[str] = []
    total_error_count = 0
    baseline_error_count = 0

    for log_group in log_groups:
        results = query_log_group(log_group, current_start, current_end, error_patterns)
        if results is None:
            continue

        queried_log_groups.append(log_group)
        raw_results = results.get("results", [])

        # Group by pattern
        pattern_groups: dict[str, list[dict[str, Any]]] = {}
        for entry in raw_results:
            message = entry.get("@message", entry.get("message", ""))
            matched_pattern = next((p for p in error_patterns if p in message), error_patterns[0] if error_patterns else "Unknown")
            pattern_groups.setdefault(matched_pattern, []).append(entry)

        for pattern, entries in pattern_groups.items():
            all_findings.append({
                "pattern": pattern,
                "count": len(entries),
                "firstSeen": current_start,
                "lastSeen": current_end,
                "sampleLines": [e.get("@message", e.get("message", "")) for e in entries[:MAX_SAMPLE_LINES]],
                "logGroup": log_group,
                "logStream": entries[0].get("@logStream") if entries else None,
                "isNew": True,  # Conservative default
            })
            total_error_count += len(entries)

        bl_count = query_baseline_count(log_group, baseline_start, baseline_end, error_patterns)
        baseline_error_count += bl_count // 7 if bl_count > 0 else 0

    if not queried_log_groups:
        return SkillResponse(status="error", message="All log groups inaccessible", data=None).model_dump(by_alias=True)

    log_finding_models = [
        LogFinding(
            pattern=f["pattern"], count=f["count"],
            firstSeen=f["firstSeen"], lastSeen=f["lastSeen"],
            isNew=f.get("isNew", False),
            severity=classify_pattern_severity(f["pattern"]),
            sampleLogLines=f.get("sampleLines", [])[:MAX_SAMPLE_LINES],
            logGroup=f["logGroup"], logStream=f.get("logStream"),
        )
        for f in all_findings
    ]

    time_range = TimeRange(start=current_start, end=current_end)
    log_findings = LogFindings(
        findings=log_finding_models, queriedLogGroups=queried_log_groups,
        timeRange=time_range, totalErrorCount=total_error_count,
        baselineErrorCount=baseline_error_count,
    )

    return SkillResponse(
        status="success",
        message=f"Log triage complete: {len(log_finding_models)} findings across {len(queried_log_groups)} groups",
        data=json.loads(log_findings.model_dump_json(by_alias=True)),
    ).model_dump(by_alias=True)
