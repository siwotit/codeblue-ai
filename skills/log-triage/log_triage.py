"""Log Triage skill for CodeBlue AI.

Queries CloudWatch Logs Insights and Kubernetes pod logs for error patterns,
compares current error counts against a 7-day baseline to classify findings
as new or pre-existing, and extracts representative log lines.

Input: LogTriageRequest JSON on stdin
Output: SkillResponse wrapping LogFindings JSON on stdout

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

# Add parent paths for imports when running via uv
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from shared.mcp_client import cloudwatch_mcp, kubernetes_mcp
from shared.models import (
    LogFinding,
    LogFindings,
    LogSeverity,
    SkillResponse,
    TimeRange,
)

# --- Constants ---

DEFAULT_ERROR_PATTERNS: list[str] = [
    "ERROR",
    "FATAL",
    "Exception",
    "Timeout",
    "ConnectionRefused",
]

MAX_SAMPLE_LINES = 5

# Patterns for pod log queries (K8s workloads)
K8S_POD_LOG_PATTERNS: list[str] = [
    "Exception",
    "OOMKilled",
    "CrashLoopBackOff",
    "Error",
    "FATAL",
    "panic",
]


# --- Severity classification ---


def classify_pattern_severity(pattern: str) -> LogSeverity:
    """Classify a log pattern into a severity level.

    Args:
        pattern: The error pattern string.

    Returns:
        LogSeverity enum value.
    """
    pattern_lower = pattern.lower()
    if "fatal" in pattern_lower or "panic" in pattern_lower:
        return LogSeverity.FATAL
    elif "warn" in pattern_lower:
        return LogSeverity.WARNING
    return LogSeverity.ERROR


# --- CloudWatch Logs Insights queries ---


def build_insights_query(patterns: list[str]) -> str:
    """Build a CloudWatch Logs Insights query to find error patterns.

    Args:
        patterns: List of error pattern strings to search for.

    Returns:
        A Logs Insights query string.
    """
    # Build a filter clause that matches any of the patterns
    filter_clauses = " or ".join(
        f'@message like /{pattern}/' for pattern in patterns
    )
    query = (
        f"fields @timestamp, @message, @logStream\n"
        f"| filter ({filter_clauses})\n"
        f"| sort @timestamp desc\n"
        f"| limit 1000"
    )
    return query


def build_count_query(patterns: list[str]) -> str:
    """Build a CloudWatch Logs Insights query to count error patterns.

    Args:
        patterns: List of error pattern strings to search for.

    Returns:
        A Logs Insights query string that returns counts per pattern.
    """
    filter_clauses = " or ".join(
        f'@message like /{pattern}/' for pattern in patterns
    )
    query = (
        f"fields @timestamp, @message\n"
        f"| filter ({filter_clauses})\n"
        f"| stats count(*) as errorCount"
    )
    return query


def query_log_group(
    log_group: str,
    start_time: datetime,
    end_time: datetime,
    patterns: list[str],
) -> dict[str, Any] | None:
    """Query a single CloudWatch log group for error patterns.

    Args:
        log_group: CloudWatch log group name.
        start_time: Start of the query window.
        end_time: End of the query window.
        patterns: Error patterns to search for.

    Returns:
        Dict with query results, or None if the log group is inaccessible.
    """
    query = build_insights_query(patterns)

    response = cloudwatch_mcp.invoke(
        "queryLogInsights",
        {
            "logGroupName": log_group,
            "query": query,
            "startTime": start_time.isoformat(),
            "endTime": end_time.isoformat(),
        },
    )

    if not response.success:
        # Log group is inaccessible — skip gracefully (Requirement 5.7)
        return None

    return response.data


def query_baseline_count(
    log_group: str,
    start_time: datetime,
    end_time: datetime,
    patterns: list[str],
) -> int:
    """Query the baseline error count for a log group in the given window.

    Args:
        log_group: CloudWatch log group name.
        start_time: Start of the baseline window.
        end_time: End of the baseline window.
        patterns: Error patterns to search for.

    Returns:
        Total error count in the baseline window, or 0 if unavailable.
    """
    query = build_count_query(patterns)

    response = cloudwatch_mcp.invoke(
        "queryLogInsights",
        {
            "logGroupName": log_group,
            "query": query,
            "startTime": start_time.isoformat(),
            "endTime": end_time.isoformat(),
        },
    )

    if not response.success:
        return 0

    # Extract count from response
    results = response.data.get("results", [])
    if results and len(results) > 0:
        first_row = results[0]
        count_val = first_row.get("errorCount", 0)
        try:
            return int(count_val)
        except (ValueError, TypeError):
            return 0

    return 0


# --- Kubernetes pod log queries ---


def query_pod_logs(
    namespace: str,
    patterns: list[str],
    start_time: datetime,
    end_time: datetime,
) -> list[dict[str, Any]]:
    """Query Kubernetes pod logs for error patterns in a namespace.

    Args:
        namespace: Kubernetes namespace to query.
        patterns: Error patterns to search for in pod logs.
        start_time: Start of the query window.
        end_time: End of the query window.

    Returns:
        List of log finding dicts with pattern, count, samples, etc.
    """
    # First get pods in the namespace
    pods_response = kubernetes_mcp.invoke(
        "getPods",
        {
            "namespace": namespace,
            "startTime": start_time.isoformat(),
            "endTime": end_time.isoformat(),
        },
    )

    if not pods_response.success:
        return []

    pods = pods_response.data.get("pods", [])
    findings: list[dict[str, Any]] = []

    for pod in pods:
        pod_name = pod.get("name", "")
        # Query pod logs for patterns
        logs_response = kubernetes_mcp.invoke(
            "getPodLogs",
            {
                "namespace": namespace,
                "podName": pod_name,
                "sinceTime": start_time.isoformat(),
            },
        )

        if not logs_response.success:
            continue

        log_lines = logs_response.data.get("logs", [])
        if isinstance(log_lines, str):
            log_lines = log_lines.splitlines()

        # Search for each pattern in the log lines
        for pattern in patterns:
            matching_lines = [
                line for line in log_lines if pattern in line
            ]
            if matching_lines:
                findings.append({
                    "pattern": pattern,
                    "count": len(matching_lines),
                    "sampleLines": matching_lines[:MAX_SAMPLE_LINES],
                    "podName": pod_name,
                    "namespace": namespace,
                })

    return findings


# --- Finding construction ---


def extract_findings_from_results(
    results: dict[str, Any],
    log_group: str,
    patterns: list[str],
    start_time: datetime,
    end_time: datetime,
) -> list[dict[str, Any]]:
    """Extract log findings from CloudWatch Logs Insights query results.

    Groups results by matching pattern, extracts sample lines, and
    computes first/last seen timestamps.

    Args:
        results: Raw query results from CloudWatch Logs Insights.
        log_group: The log group queried.
        patterns: The error patterns searched for.
        start_time: Start of the query window.
        end_time: End of the query window.

    Returns:
        List of finding dictionaries ready for classification.
    """
    raw_results = results.get("results", [])
    if not raw_results:
        return []

    # Group log entries by matched pattern
    pattern_groups: dict[str, list[dict[str, Any]]] = {}
    for entry in raw_results:
        message = entry.get("@message", entry.get("message", ""))
        timestamp_str = entry.get("@timestamp", entry.get("timestamp", ""))
        log_stream = entry.get("@logStream", entry.get("logStream", None))

        # Determine which pattern this entry matches
        matched_pattern = None
        for pattern in patterns:
            if pattern in message:
                matched_pattern = pattern
                break

        if matched_pattern is None:
            # If no exact match, use the first pattern as fallback
            matched_pattern = patterns[0] if patterns else "Unknown"

        if matched_pattern not in pattern_groups:
            pattern_groups[matched_pattern] = []

        pattern_groups[matched_pattern].append({
            "message": message,
            "timestamp": timestamp_str,
            "logStream": log_stream,
        })

    # Build findings from grouped results
    findings: list[dict[str, Any]] = []
    for pattern, entries in pattern_groups.items():
        # Parse timestamps to find first/last seen
        timestamps: list[datetime] = []
        for entry in entries:
            ts_str = entry.get("timestamp", "")
            if ts_str:
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    timestamps.append(ts)
                except (ValueError, TypeError):
                    pass

        first_seen = min(timestamps) if timestamps else start_time
        last_seen = max(timestamps) if timestamps else end_time

        # Extract sample log lines (max 5 per Requirement 5.5)
        sample_lines = [
            entry["message"] for entry in entries[:MAX_SAMPLE_LINES]
            if entry.get("message")
        ]

        # Get log stream from first entry
        log_stream = entries[0].get("logStream") if entries else None

        findings.append({
            "pattern": pattern,
            "count": len(entries),
            "firstSeen": first_seen,
            "lastSeen": last_seen,
            "sampleLines": sample_lines,
            "logGroup": log_group,
            "logStream": log_stream,
        })

    return findings


def classify_finding_as_new(
    pattern: str,
    log_group: str,
    baseline_start: datetime,
    baseline_end: datetime,
    patterns: list[str],
) -> bool:
    """Determine if a finding is new (not present in 7-day baseline).

    Requirement 5.4: isNew = true when pattern not found in baseline.

    Args:
        pattern: The error pattern to check.
        log_group: The log group to query.
        baseline_start: Start of the 7-day baseline window.
        baseline_end: End of the 7-day baseline window.
        patterns: Full list of patterns (used for query construction).

    Returns:
        True if the pattern is new (not in baseline), False if pre-existing.
    """
    # Query baseline for this specific pattern
    query = (
        f"fields @timestamp, @message\n"
        f"| filter @message like /{pattern}/\n"
        f"| stats count(*) as patternCount"
    )

    response = cloudwatch_mcp.invoke(
        "queryLogInsights",
        {
            "logGroupName": log_group,
            "query": query,
            "startTime": baseline_start.isoformat(),
            "endTime": baseline_end.isoformat(),
        },
    )

    if not response.success:
        # If we can't query baseline, assume pre-existing (conservative)
        return False

    results = response.data.get("results", [])
    if results and len(results) > 0:
        first_row = results[0]
        count_val = first_row.get("patternCount", 0)
        try:
            baseline_count = int(count_val)
            return baseline_count == 0
        except (ValueError, TypeError):
            return False

    # No results means pattern was not found in baseline → it's new
    return True


# --- Main skill logic ---


def run_log_triage(request: dict[str, Any]) -> dict[str, Any]:
    """Execute the log triage analysis.

    Args:
        request: LogTriageRequest dictionary with keys:
            - logGroups (list[str]): CloudWatch log group names to query
            - namespaces (list[str], optional): K8s namespaces for pod logs
            - timeRange (dict): {start, end} ISO 8601 timestamps
            - errorPatterns (list[str], optional): Override default patterns

    Returns:
        SkillResponse dictionary with status, message, and data fields.
    """
    # Validate required fields
    if "logGroups" not in request:
        return SkillResponse(
            status="error",
            message="Missing required field: logGroups",
            data=None,
        ).model_dump(by_alias=True)

    if "timeRange" not in request:
        return SkillResponse(
            status="error",
            message="Missing required field: timeRange",
            data=None,
        ).model_dump(by_alias=True)

    log_groups = request["logGroups"]
    if not log_groups or not isinstance(log_groups, list):
        return SkillResponse(
            status="error",
            message="logGroups must be a non-empty list",
            data=None,
        ).model_dump(by_alias=True)

    # Validate log group names (must start with /)
    invalid_groups = [lg for lg in log_groups if not isinstance(lg, str) or not lg.startswith("/")]
    if invalid_groups:
        return SkillResponse(
            status="error",
            message=f"Invalid log group names (must start with '/'): {', '.join(str(g) for g in invalid_groups)}",
            data=None,
        ).model_dump(by_alias=True)

    namespaces = request.get("namespaces", [])
    error_patterns = request.get("errorPatterns", DEFAULT_ERROR_PATTERNS)

    # Parse time range
    time_range_raw = request["timeRange"]
    try:
        current_start = datetime.fromisoformat(
            time_range_raw["start"].replace("Z", "+00:00")
        )
        current_end = datetime.fromisoformat(
            time_range_raw["end"].replace("Z", "+00:00")
        )
    except (KeyError, ValueError, TypeError) as e:
        return SkillResponse(
            status="error",
            message=f"Invalid timeRange: {e}",
            data=None,
        ).model_dump(by_alias=True)

    # Compute baseline window: same time-of-day window from 7 days ago
    # Requirement 5.3: Compare against same window from previous 7 days
    window_duration = current_end - current_start
    baseline_end = current_start  # Baseline ends where current window starts
    baseline_start = baseline_end - timedelta(days=7)

    # --- Query CloudWatch log groups (Requirement 5.1) ---
    all_findings: list[dict[str, Any]] = []
    queried_log_groups: list[str] = []
    inaccessible_groups: list[str] = []
    total_error_count = 0
    baseline_error_count = 0

    for log_group in log_groups:
        # Query current window
        results = query_log_group(log_group, current_start, current_end, error_patterns)

        if results is None:
            # Log group inaccessible — skip gracefully (Requirement 5.7)
            inaccessible_groups.append(log_group)
            continue

        queried_log_groups.append(log_group)

        # Extract findings from results
        findings = extract_findings_from_results(
            results, log_group, error_patterns, current_start, current_end
        )

        # Get baseline error count (Requirement 5.3)
        baseline_count = query_baseline_count(
            log_group, baseline_start, baseline_end, error_patterns
        )
        # Average the 7-day count to per-window equivalent
        baseline_per_window = baseline_count // 7 if baseline_count > 0 else 0
        baseline_error_count += baseline_per_window

        # Classify each finding as new or pre-existing (Requirement 5.4)
        for finding in findings:
            is_new = classify_finding_as_new(
                finding["pattern"],
                log_group,
                baseline_start,
                baseline_end,
                error_patterns,
            )
            finding["isNew"] = is_new
            total_error_count += finding["count"]

        all_findings.extend(findings)

    # --- Query Kubernetes pod logs (Requirement 5.2) ---
    if namespaces:
        for namespace in namespaces:
            pod_findings = query_pod_logs(
                namespace, K8S_POD_LOG_PATTERNS, current_start, current_end
            )
            for pf in pod_findings:
                # Check if this pattern already exists in findings
                existing = next(
                    (f for f in all_findings if f["pattern"] == pf["pattern"]),
                    None,
                )
                if existing:
                    existing["count"] += pf["count"]
                    # Append new sample lines up to max
                    remaining_slots = MAX_SAMPLE_LINES - len(existing.get("sampleLines", []))
                    if remaining_slots > 0:
                        existing.setdefault("sampleLines", []).extend(
                            pf["sampleLines"][:remaining_slots]
                        )
                else:
                    all_findings.append({
                        "pattern": pf["pattern"],
                        "count": pf["count"],
                        "firstSeen": current_start,
                        "lastSeen": current_end,
                        "sampleLines": pf["sampleLines"][:MAX_SAMPLE_LINES],
                        "logGroup": f"k8s/{namespace}",
                        "logStream": pf.get("podName"),
                        "isNew": True,  # K8s pod log patterns without baseline are treated as new
                    })
                total_error_count += pf["count"]

    # --- Build response ---

    # If all log groups were inaccessible, return error (Requirement 5.7)
    if not queried_log_groups and not namespaces:
        return SkillResponse(
            status="error",
            message=(
                f"Log triage failed: all specified log groups are inaccessible "
                f"({', '.join(inaccessible_groups)})"
            ),
            data=None,
        ).model_dump(by_alias=True)

    # Build LogFinding models
    log_finding_models: list[LogFinding] = []
    for finding in all_findings:
        first_seen = finding.get("firstSeen", current_start)
        last_seen = finding.get("lastSeen", current_end)

        # Ensure datetime objects
        if isinstance(first_seen, str):
            first_seen = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
        if isinstance(last_seen, str):
            last_seen = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))

        log_finding_models.append(
            LogFinding(
                pattern=finding["pattern"],
                count=finding["count"],
                firstSeen=first_seen,
                lastSeen=last_seen,
                isNew=finding.get("isNew", False),
                severity=classify_pattern_severity(finding["pattern"]),
                sampleLogLines=finding.get("sampleLines", [])[:MAX_SAMPLE_LINES],
                logGroup=finding["logGroup"],
                logStream=finding.get("logStream"),
            )
        )

    # Build LogFindings response
    time_range = TimeRange(start=current_start, end=current_end)
    log_findings = LogFindings(
        findings=log_finding_models,
        queriedLogGroups=queried_log_groups,
        timeRange=time_range,
        totalErrorCount=total_error_count,
        baselineErrorCount=baseline_error_count,
    )

    # Build message
    new_count = sum(1 for f in log_finding_models if f.is_new)
    preexisting_count = len(log_finding_models) - new_count
    message_parts = [
        f"Log triage complete: {len(log_finding_models)} findings across "
        f"{len(queried_log_groups)} log groups "
        f"({new_count} new, {preexisting_count} pre-existing)"
    ]
    if inaccessible_groups:
        message_parts.append(
            f" ({len(inaccessible_groups)} groups inaccessible: "
            f"{', '.join(inaccessible_groups)})"
        )

    message = "".join(message_parts)

    # Build success response
    response = SkillResponse(
        status="success",
        message=message,
        data=json.loads(log_findings.model_dump_json(by_alias=True)),
    )

    return response.model_dump(by_alias=True)


def main() -> None:
    """Entry point: read LogTriageRequest from stdin, write SkillResponse to stdout."""
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
            result = run_log_triage(request)
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
