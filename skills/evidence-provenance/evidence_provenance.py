"""Evidence Provenance Skill — generates traceable links for incident claims.

Accepts JSON on stdin containing findings with claims and source details.
Generates CloudWatch console deep links, kubectl verification commands,
and CloudTrail console links with ISO 8601 timestamps and query parameters.
Validates that evidence timestamps fall within declared query time ranges.
Annotates claims where provenance cannot be resolved with an unavailability reason.

Usage:
    echo '{"findings": [...], "region": "us-east-1"}' | uv run evidence_provenance.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RECOGNIZED_SOURCES = {"cloudwatch", "kubernetes", "cloudtrail", "grafana"}

# Required fields per source type for provenance resolution
_REQUIRED_FIELDS: dict[str, list[str]] = {
    "cloudwatch": ["metricName", "metricNamespace"],
    "kubernetes": ["resourceKind", "resourceName", "namespace", "clusterContext"],
    "cloudtrail": ["eventId"],
    "grafana": [],  # Grafana has no mandatory fields but has limited provenance
}

# kubectl command verb mapping based on resource kind
_KUBECTL_VERB_MAP: dict[str, str] = {
    "pod": "describe",
    "deployment": "get",
    "replicaset": "get",
    "statefulset": "get",
    "daemonset": "get",
    "service": "get",
    "event": "get events",
    "node": "describe",
    "configmap": "get",
    "secret": "get",
    "ingress": "get",
    "hpa": "get",
    "job": "get",
    "cronjob": "get",
}


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _parse_iso_timestamp(ts: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp string into a datetime object.

    Returns None if the string is None or cannot be parsed.
    """
    if not ts:
        return None
    try:
        # Handle 'Z' suffix and various ISO formats
        normalized = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return None


def _is_timestamp_within_range(
    timestamp: str | None,
    time_range: dict[str, str] | None,
) -> tuple[bool, str | None]:
    """Validate that a timestamp falls within the declared time range.

    Returns (is_valid, error_reason).
    - (True, None) if valid or if validation cannot be performed (missing data).
    - (False, reason) if the timestamp is outside the range.
    """
    if not timestamp or not time_range:
        return True, None

    ts = _parse_iso_timestamp(timestamp)
    start = _parse_iso_timestamp(time_range.get("start"))
    end = _parse_iso_timestamp(time_range.get("end"))

    if ts is None or start is None or end is None:
        return True, None  # Cannot validate — don't block

    if ts < start or ts > end:
        start_str = time_range.get("start", "")
        end_str = time_range.get("end", "")
        return (
            False,
            f"Evidence timestamp {timestamp} falls outside declared query time range [{start_str}, {end_str}]",
        )

    return True, None


# ---------------------------------------------------------------------------
# URL generation: CloudWatch
# ---------------------------------------------------------------------------


def _generate_cloudwatch_url(
    source_details: dict[str, Any],
    region: str,
) -> str:
    """Generate a CloudWatch console deep link for metric graphs.

    URL format:
    https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#metricsV2:graph=~(...)
    """
    metric_name = source_details.get("metricName", "")
    namespace = source_details.get("metricNamespace", "")
    dimensions = source_details.get("dimensions", {})
    time_range = source_details.get("timeRange", {})

    start_iso = time_range.get("start", "")
    end_iso = time_range.get("end", "")

    # Build the dimensions part of the metrics array
    dim_parts: list[str] = []
    for key, value in dimensions.items():
        dim_parts.append(f"~'{key}~'{value}")
    dim_str = "".join(dim_parts)

    # Build the graph definition
    graph_def = (
        f"~(metrics~(~(~'{namespace}~'{metric_name}{dim_str}))"
        f"~view~'timeSeries"
        f"~stacked~false"
        f"~region~'{region}"
        f"~start~'{start_iso}"
        f"~end~'{end_iso}"
        f"~period~300)"
    )

    url = (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={region}#metricsV2:graph={graph_def}"
    )

    return url


# ---------------------------------------------------------------------------
# URL generation: CloudTrail
# ---------------------------------------------------------------------------


def _generate_cloudtrail_url(
    source_details: dict[str, Any],
    region: str,
) -> str:
    """Generate a CloudTrail console detail page link.

    URL format:
    https://{region}.console.aws.amazon.com/cloudtrailv2/home?region={region}#/events/{eventId}
    """
    event_id = source_details.get("eventId", "")
    url = (
        f"https://{region}.console.aws.amazon.com/cloudtrailv2/home"
        f"?region={region}#/events/{event_id}"
    )
    return url


# ---------------------------------------------------------------------------
# Command generation: kubectl
# ---------------------------------------------------------------------------


def _generate_kubectl_command(
    source_details: dict[str, Any],
) -> str:
    """Generate a kubectl verification command for a Kubernetes resource.

    Command selection logic:
    - Pods: kubectl describe (shows events, conditions, container status)
    - Deployments/ReplicaSets: kubectl get -o yaml (shows rollout state)
    - Events: kubectl get events with time-based sorting
    - Other: kubectl describe (general-purpose)
    """
    resource_kind = source_details.get("resourceKind", "").lower()
    resource_name = source_details.get("resourceName", "")
    namespace = source_details.get("namespace", "")
    cluster_context = source_details.get("clusterContext", "")

    # Determine the appropriate verb based on resource kind
    if resource_kind == "event":
        # For events, use get events with sorting
        cmd = (
            f"kubectl get events"
            f" -n {namespace}"
            f" --context {cluster_context}"
            f" --sort-by='.lastTimestamp'"
        )
    elif resource_kind == "pod":
        cmd = (
            f"kubectl describe pod {resource_name}"
            f" -n {namespace}"
            f" --context {cluster_context}"
        )
    elif resource_kind in ("deployment", "replicaset", "statefulset", "daemonset"):
        cmd = (
            f"kubectl get {resource_kind} {resource_name}"
            f" -n {namespace}"
            f" --context {cluster_context}"
            f" -o yaml"
        )
    else:
        # Default: describe
        cmd = (
            f"kubectl describe {resource_kind} {resource_name}"
            f" -n {namespace}"
            f" --context {cluster_context}"
        )

    return cmd


# ---------------------------------------------------------------------------
# Core provenance annotation logic
# ---------------------------------------------------------------------------


def _annotate_finding(
    finding: dict[str, Any],
    default_region: str,
) -> dict[str, Any]:
    """Annotate a single finding with provenance information.

    Returns a ProvenanceAnnotatedFinding dict.
    """
    claim = finding.get("claim", "")
    source = finding.get("source", "")
    timestamp = finding.get("timestamp")
    source_details = finding.get("sourceDetails") or {}

    # Determine region (source-level override or default)
    region = source_details.get("region") or default_region
    time_range = source_details.get("timeRange")

    # Base annotated finding
    annotated: dict[str, Any] = {
        "claim": claim,
        "source": source,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "provenanceResolved": False,
        "consoleUrl": None,
        "verificationCommand": None,
        "queryParameters": {
            "timeRange": time_range if time_range else {"start": None, "end": None},
            "filters": {},
        },
        "unavailabilityReason": None,
    }

    # --- Check source type ---
    if source not in _RECOGNIZED_SOURCES:
        annotated["unavailabilityReason"] = f"Unrecognized source system: {source}"
        return annotated

    # --- Check required fields for the source type ---
    required = _REQUIRED_FIELDS.get(source, [])
    missing_fields: list[str] = []
    for field in required:
        if not source_details.get(field):
            missing_fields.append(field)

    if missing_fields:
        field_list = ", ".join(missing_fields)
        annotated["unavailabilityReason"] = (
            f"Insufficient source details to generate provenance link: missing {field_list}"
        )
        return annotated

    # --- Timestamp validation ---
    if timestamp and time_range:
        is_valid, reason = _is_timestamp_within_range(timestamp, time_range)
        if not is_valid:
            annotated["unavailabilityReason"] = reason
            return annotated
    elif not time_range and source in ("cloudwatch", "cloudtrail"):
        # Time-sensitive sources need a time range
        annotated["unavailabilityReason"] = (
            "No query time range declared for time-sensitive source"
        )
        return annotated

    # --- Generate provenance based on source type ---
    if source == "cloudwatch":
        console_url = _generate_cloudwatch_url(source_details, region)
        annotated["consoleUrl"] = console_url
        annotated["provenanceResolved"] = True

        # Populate query parameters
        dimensions = source_details.get("dimensions", {})
        annotated["queryParameters"] = {
            "timeRange": time_range,
            "filters": {
                "metricName": source_details.get("metricName", ""),
                "metricNamespace": source_details.get("metricNamespace", ""),
                **{f"dim:{k}": v for k, v in dimensions.items()},
            },
        }

    elif source == "cloudtrail":
        console_url = _generate_cloudtrail_url(source_details, region)
        annotated["consoleUrl"] = console_url
        annotated["provenanceResolved"] = True

        annotated["queryParameters"] = {
            "timeRange": time_range,
            "filters": {
                "eventId": source_details.get("eventId", ""),
                "region": region,
            },
        }

    elif source == "kubernetes":
        kubectl_cmd = _generate_kubectl_command(source_details)
        annotated["verificationCommand"] = kubectl_cmd
        annotated["provenanceResolved"] = True

        annotated["queryParameters"] = {
            "timeRange": time_range if time_range else {"start": None, "end": None},
            "filters": {
                "resourceKind": source_details.get("resourceKind", ""),
                "resourceName": source_details.get("resourceName", ""),
                "namespace": source_details.get("namespace", ""),
                "clusterContext": source_details.get("clusterContext", ""),
            },
        }

    elif source == "grafana":
        # Grafana has limited provenance — we can't generate a specific URL
        # without a dashboard ID or panel ID. Mark as unresolvable unless
        # dashboard info is provided.
        dashboard_url = source_details.get("dashboardUrl")
        if dashboard_url:
            annotated["consoleUrl"] = dashboard_url
            annotated["provenanceResolved"] = True
            annotated["queryParameters"] = {
                "timeRange": time_range if time_range else {"start": None, "end": None},
                "filters": {
                    "dashboardUrl": dashboard_url,
                },
            }
        else:
            annotated["unavailabilityReason"] = (
                "Insufficient source details to generate provenance link: "
                "missing dashboardUrl for Grafana source"
            )

    return annotated


# ---------------------------------------------------------------------------
# Main processing function
# ---------------------------------------------------------------------------


def annotate_findings(input_data: dict[str, Any]) -> dict[str, Any]:
    """Process all findings and return annotated results.

    Validates the input structure, iterates over each finding, and
    produces a ProvenanceAnnotatedFinding for each one. Never fails
    the entire batch due to individual finding failures.
    """
    # --- Validate required top-level fields ---
    if "findings" not in input_data:
        return _error("Required field 'findings' is missing from input")

    if not isinstance(input_data["findings"], list):
        return _error("Field 'findings' must be an array")

    region = input_data.get("region")
    if not region:
        return _error("Required field 'region' is missing")

    findings = input_data["findings"]

    # --- Process each finding ---
    annotated_findings: list[dict[str, Any]] = []
    resolved_count = 0
    unresolved_count = 0

    for finding in findings:
        annotated = _annotate_finding(finding, region)
        annotated_findings.append(annotated)

        if annotated["provenanceResolved"]:
            resolved_count += 1
        else:
            unresolved_count += 1

    # --- Build response ---
    data = {
        "annotatedFindings": annotated_findings,
        "summary": {
            "totalFindings": len(findings),
            "resolvedCount": resolved_count,
            "unresolvedCount": unresolved_count,
        },
    }

    return _success(data=data)


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _success(data: dict[str, Any], message: str = "") -> dict[str, Any]:
    """Wrap data in a success SkillResponse."""
    return {"status": "success", "message": message, "data": data}


def _error(message: str) -> dict[str, Any]:
    """Wrap message in an error SkillResponse."""
    return {"status": "error", "message": message, "data": None}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Read JSON from stdin, process, and write result to stdout."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            result = _error("Empty input received on stdin")
        else:
            input_data = json.loads(raw)
            result = annotate_findings(input_data)
    except json.JSONDecodeError as e:
        result = _error(f"Invalid JSON input: {e}")
    except Exception as e:
        result = _error(f"Unexpected error during evidence provenance annotation: {e}")

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
