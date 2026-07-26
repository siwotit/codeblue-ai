"""Deploy Correlation Skill — discovers recent changes that may have caused an incident.

Queries CloudTrail for API calls modifying resources in the blast radius and
Kubernetes for rollouts, replica changes, and HPA scaling events within a
configurable lookback window (default 24h). Scores each change by temporal
proximity to the incident start time and resource overlap with affected resources.

Input: CorrelationRequest JSON on stdin
Output: SkillResponse wrapping CorrelatedChanges JSON on stdout

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

# Add parent paths for imports when running via uv
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from shared.mcp_client import aws_api_mcp, kubernetes_mcp
from shared.models import (
    Change,
    ChangeSource,
    ChangeType,
    CorrelatedChanges,
    ResourceIdentifier,
    SkillResponse,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# CloudTrail event names that indicate modifying API calls
_WRITE_EVENT_PATTERNS: list[str] = [
    "Create",
    "Update",
    "Delete",
    "Put",
    "Modify",
    "Register",
    "Deregister",
    "Run",
    "Start",
    "Stop",
    "Terminate",
    "Deploy",
    "Scale",
]

# Kubernetes event reasons indicating changes
_K8S_CHANGE_REASONS: set[str] = {
    "ScalingReplicaSet",
    "SuccessfulCreate",
    "SuccessfulDelete",
    "DeploymentRollback",
    "DeploymentUpdated",
    "ScaledUp",
    "ScaledDown",
    "SuccessfulRescale",
    "NewReplicaSetCreated",
    "RolloutCompleted",
}

# Default lookback window in hours
_DEFAULT_LOOKBACK_HOURS = 24


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------


def parse_lookback_window(window: str) -> timedelta:
    """Parse a lookback window string like '24h' or '48h' into a timedelta.

    Args:
        window: Duration string (e.g., "24h", "48h", "12h").

    Returns:
        timedelta representing the lookback window.
    """
    match = re.match(r"^(\d+)h$", window.strip())
    if match:
        return timedelta(hours=int(match.group(1)))
    # Default to 24h if unparseable
    return timedelta(hours=_DEFAULT_LOOKBACK_HOURS)


# ---------------------------------------------------------------------------
# Temporal proximity scoring (Requirement 4.3)
# ---------------------------------------------------------------------------


def compute_temporal_proximity(
    change_time: datetime,
    incident_time: datetime,
    lookback_seconds: float,
) -> float:
    """Compute temporal proximity score for a change.

    Formula: max(0.0, 1.0 - (timeDeltaSeconds / lookbackWindowSeconds))

    Changes closer to the incident receive higher scores.

    Args:
        change_time: Timestamp of the change.
        incident_time: Incident start time.
        lookback_seconds: Total lookback window in seconds.

    Returns:
        Temporal proximity score between 0.0 and 1.0.
    """
    if lookback_seconds <= 0:
        return 0.0
    time_delta_seconds = abs((incident_time - change_time).total_seconds())
    proximity = max(0.0, 1.0 - (time_delta_seconds / lookback_seconds))
    return round(proximity, 4)


# ---------------------------------------------------------------------------
# Resource overlap detection (Requirement 4.4)
# ---------------------------------------------------------------------------


def check_resource_overlap(
    change_resource: str,
    affected_resources: list[dict[str, Any]],
) -> bool:
    """Determine whether a change's affected resource overlaps with incident resources.

    Resource overlap is True when at least one incident resource identifier
    matches or is contained within the change's affected resource identifier.

    Args:
        change_resource: The resource identifier of the change (ARN, k8s path, etc.)
        affected_resources: List of ResourceIdentifier dicts from the request.

    Returns:
        True if overlap detected, False otherwise.
    """
    if not change_resource or not affected_resources:
        return False

    change_lower = change_resource.lower()

    for resource in affected_resources:
        resource_value = resource.get("value", "").lower()
        if not resource_value:
            continue

        # Exact match
        if change_lower == resource_value:
            return True

        # Substring containment in either direction
        if resource_value in change_lower or change_lower in resource_value:
            return True

    return False


# ---------------------------------------------------------------------------
# Correlation score computation (Requirements 4.5, 4.6)
# ---------------------------------------------------------------------------


def compute_correlation_score(
    temporal_proximity: float,
    resource_overlap: bool,
) -> float:
    """Compute final correlation score combining temporal proximity and resource overlap.

    Key invariant (Requirement 4.6): When BOTH temporal proximity is high AND
    resource overlap is present, the score MUST be higher than when only one
    factor is present.

    Scoring strategy:
    - Both factors: weighted combination that boosts above either alone
    - Only temporal proximity: reduced by a factor (capped lower)
    - Only resource overlap: base relevance score reduced by weak temporal signal

    Args:
        temporal_proximity: 0.0-1.0 score for time closeness.
        resource_overlap: Whether the change touches incident resources.

    Returns:
        Correlation score between 0.0 and 1.0.
    """
    if resource_overlap:
        # Both factors present: boost score
        # Formula: 0.4 * proximity + 0.5 * overlap_base + 0.1 * proximity * overlap_bonus
        # This ensures that higher proximity + overlap > either alone
        overlap_base = 0.5
        score = (0.5 * temporal_proximity) + (overlap_base * 0.7) + (0.15 * temporal_proximity)
        # Clamp to [0.0, 1.0]
        score = min(1.0, max(0.0, score))
    else:
        # Only temporal proximity, no resource overlap
        # Cap effective score lower to satisfy Req 4.6
        score = temporal_proximity * 0.6

    return round(score, 4)


# ---------------------------------------------------------------------------
# Change type classification
# ---------------------------------------------------------------------------


def classify_cloudtrail_event(event_name: str) -> ChangeType:
    """Classify a CloudTrail event name into a ChangeType.

    Args:
        event_name: The CloudTrail event name (e.g., "UpdateService", "PutScalingPolicy").

    Returns:
        Appropriate ChangeType enum value.
    """
    lower = event_name.lower()

    if any(kw in lower for kw in ["deploy", "updateservice", "updatefunction", "createdeployment"]):
        return ChangeType.DEPLOYMENT

    # Check scaling before IAM — "PutScalingPolicy" is a scaling event, not IAM
    if any(kw in lower for kw in ["scaling", "autoscaling", "capacity", "scaletarget"]):
        return ChangeType.SCALING_EVENT

    if any(kw in lower for kw in ["iam", "role", "permission"]):
        return ChangeType.IAM_CHANGE

    # "policy" without scaling context is IAM
    if "policy" in lower and "scaling" not in lower:
        return ChangeType.IAM_CHANGE

    if any(kw in lower for kw in ["addon", "nodegroup"]):
        return ChangeType.ADDON_UPDATE

    return ChangeType.CONFIG_CHANGE


def classify_k8s_event(reason: str, kind: str) -> ChangeType:
    """Classify a Kubernetes event into a ChangeType.

    Args:
        reason: The K8s event reason.
        kind: The involved object kind.

    Returns:
        Appropriate ChangeType enum value.
    """
    lower_reason = reason.lower()

    if any(kw in lower_reason for kw in ["rollout", "deployment", "replicaset"]):
        return ChangeType.ROLLOUT

    if any(kw in lower_reason for kw in ["scale", "replica", "rescale"]):
        return ChangeType.SCALING_EVENT

    if kind.lower() in ("deployment", "replicaset", "statefulset"):
        return ChangeType.ROLLOUT

    return ChangeType.CONFIG_CHANGE


# ---------------------------------------------------------------------------
# CloudTrail query (Requirement 4.1)
# ---------------------------------------------------------------------------


def query_cloudtrail_changes(
    affected_resources: list[dict[str, Any]],
    start_time: datetime,
    end_time: datetime,
) -> tuple[list[dict[str, Any]], str | None]:
    """Query CloudTrail for modifying API calls within the lookback window.

    Args:
        affected_resources: List of ResourceIdentifier dicts.
        start_time: Start of the lookback window.
        end_time: Incident time (end of window).

    Returns:
        Tuple of (list of raw change dicts, error message or None).
    """
    changes: list[dict[str, Any]] = []

    # Build lookup attributes from resource ARNs
    resource_arns = [
        r["value"] for r in affected_resources if r.get("type") == "arn"
    ]

    try:
        # Query CloudTrail using aws-api-mcp
        lookup_params: dict[str, Any] = {
            "StartTime": start_time.isoformat(),
            "EndTime": end_time.isoformat(),
            "MaxResults": 50,
        }

        # Add resource filter if ARNs available
        if resource_arns:
            lookup_params["LookupAttributes"] = [
                {"AttributeKey": "ResourceName", "AttributeValue": arn}
                for arn in resource_arns[:5]  # Limit to avoid overly broad queries
            ]

        response = aws_api_mcp.invoke("lookupCloudTrailEvents", lookup_params)

        if not response.success:
            return [], f"CloudTrail query failed: {response.error}"

        events = response.data.get("Events", [])

        for event in events:
            event_name = event.get("EventName", "")

            # Filter to write/modify events only
            if not any(pattern.lower() in event_name.lower() for pattern in _WRITE_EVENT_PATTERNS):
                continue

            event_time = event.get("EventTime", "")
            username = event.get("Username", "unknown")
            resources_list = event.get("Resources", [])
            event_id = event.get("EventId", "")

            # Determine affected resource from event
            affected_resource = ""
            if resources_list:
                affected_resource = resources_list[0].get("ResourceName", "")
            elif resource_arns:
                affected_resource = resource_arns[0]

            # Build description from event data
            resource_type = ""
            if resources_list:
                resource_type = resources_list[0].get("ResourceType", "")

            description = f"{event_name} on {resource_type or 'resource'}"
            if affected_resource:
                description += f" — {affected_resource}"

            changes.append({
                "id": f"ct-{event_id}" if event_id else f"ct-{hashlib.sha256(f'{event_name}:{event_time}'.encode()).hexdigest()[:12]}",
                "type": classify_cloudtrail_event(event_name).value,
                "source": ChangeSource.CLOUDTRAIL.value,
                "timestamp": event_time,
                "actor": username,
                "description": description,
                "affectedResource": affected_resource,
                "details": {
                    "eventName": event_name,
                    "eventId": event_id,
                    "resourceType": resource_type,
                    "resources": resources_list,
                },
            })

    except Exception as e:
        return [], f"CloudTrail query failed: {type(e).__name__}: {e}"

    return changes, None


# ---------------------------------------------------------------------------
# Kubernetes query (Requirement 4.2)
# ---------------------------------------------------------------------------


def query_kubernetes_changes(
    affected_resources: list[dict[str, Any]],
    start_time: datetime,
    end_time: datetime,
) -> tuple[list[dict[str, Any]], str | None]:
    """Query Kubernetes for rollouts, replica changes, and HPA scaling events.

    Args:
        affected_resources: List of ResourceIdentifier dicts.
        start_time: Start of the lookback window.
        end_time: Incident time (end of window).

    Returns:
        Tuple of (list of raw change dicts, error message or None).
    """
    changes: list[dict[str, Any]] = []

    # Extract namespaces from k8s resources
    namespaces: set[str] = set()
    for resource in affected_resources:
        if resource.get("type") == "k8s_resource":
            parts = resource.get("value", "").split("/")
            if parts:
                namespaces.add(parts[0])

    # Default to "default" namespace if none found
    if not namespaces:
        namespaces = {"default"}

    try:
        for namespace in namespaces:
            # Query events related to deployments, scaling, and rollouts
            response = kubernetes_mcp.invoke(
                "getEvents",
                {
                    "namespace": namespace,
                    "fieldSelector": "type=Normal",
                    "startTime": start_time.isoformat(),
                    "endTime": end_time.isoformat(),
                },
            )

            if not response.success:
                return [], f"Kubernetes query failed: {response.error}"

            events = response.data.get("items", [])

            for event in events:
                reason = event.get("reason", "")

                # Filter to change-related events only
                if reason not in _K8S_CHANGE_REASONS:
                    continue

                involved_object = event.get("involvedObject", {})
                kind = involved_object.get("kind", "")
                name = involved_object.get("name", "")
                event_namespace = involved_object.get("namespace", namespace)
                event_time = event.get("lastTimestamp") or event.get("firstTimestamp", "")
                message = event.get("message", "")

                # Build resource path
                affected_resource = f"{event_namespace}/{kind}/{name}" if kind and name else ""

                # Generate unique ID
                uid = event.get("metadata", {}).get("uid", "")
                change_id = f"k8s-{uid}" if uid else f"k8s-{hashlib.sha256(f'{reason}:{event_time}:{name}'.encode()).hexdigest()[:12]}"

                changes.append({
                    "id": change_id,
                    "type": classify_k8s_event(reason, kind).value,
                    "source": ChangeSource.KUBERNETES.value,
                    "timestamp": event_time,
                    "actor": event.get("source", {}).get("component", "kubernetes"),
                    "description": f"{reason}: {message}" if message else reason,
                    "affectedResource": affected_resource,
                    "details": {
                        "reason": reason,
                        "kind": kind,
                        "name": name,
                        "namespace": event_namespace,
                        "message": message,
                        "count": event.get("count", 1),
                    },
                })

    except Exception as e:
        return [], f"Kubernetes query failed: {type(e).__name__}: {e}"

    return changes, None


# ---------------------------------------------------------------------------
# Main correlation logic
# ---------------------------------------------------------------------------


def run_deploy_correlation(request: dict[str, Any]) -> dict[str, Any]:
    """Execute the deploy correlation skill.

    Args:
        request: CorrelationRequest dictionary with keys:
            - affectedResources (list): ResourceIdentifier objects
            - incidentTime (str): ISO 8601 incident start time
            - lookbackWindow (str): Duration string (e.g., "24h")

    Returns:
        SkillResponse dictionary.
    """
    # Validate required fields
    affected_resources = request.get("affectedResources")
    incident_time_str = request.get("incidentTime")

    if not affected_resources:
        return SkillResponse(
            status="error",
            message="Missing required field: 'affectedResources'",
            data=None,
        ).model_dump(by_alias=True)

    if not incident_time_str:
        return SkillResponse(
            status="error",
            message="Missing required field: 'incidentTime'",
            data=None,
        ).model_dump(by_alias=True)

    # Parse incident time
    try:
        incident_time = datetime.fromisoformat(
            incident_time_str.replace("Z", "+00:00")
        )
    except (ValueError, TypeError) as e:
        return SkillResponse(
            status="error",
            message=f"Invalid incidentTime format: {e}",
            data=None,
        ).model_dump(by_alias=True)

    # Parse lookback window
    lookback_window_str = request.get("lookbackWindow", "24h")
    lookback_delta = parse_lookback_window(lookback_window_str)
    lookback_seconds = lookback_delta.total_seconds()

    # Compute time range
    start_time = incident_time - lookback_delta

    # Query both data sources, handling failures gracefully (Requirement 4.8)
    unavailable_sources: list[str] = []

    ct_changes, ct_error = query_cloudtrail_changes(
        affected_resources, start_time, incident_time
    )
    if ct_error:
        unavailable_sources.append("cloudtrail")

    k8s_changes, k8s_error = query_kubernetes_changes(
        affected_resources, start_time, incident_time
    )
    if k8s_error:
        unavailable_sources.append("kubernetes")

    # If both sources failed, return error (Requirement 4.8)
    if len(unavailable_sources) == 2:
        error_details = []
        if ct_error:
            error_details.append(f"CloudTrail ({ct_error})")
        if k8s_error:
            error_details.append(f"Kubernetes ({k8s_error})")
        return SkillResponse(
            status="error",
            message=f"All change correlation sources unavailable: {', '.join(error_details)}",
            data=None,
        ).model_dump(by_alias=True)

    # Combine all raw changes
    all_raw_changes = ct_changes + k8s_changes

    # Score each change (Requirements 4.3, 4.4, 4.5, 4.6)
    scored_changes: list[dict[str, Any]] = []

    for raw_change in all_raw_changes:
        # Parse change timestamp
        change_time_str = raw_change.get("timestamp", "")
        try:
            if isinstance(change_time_str, str):
                change_time = datetime.fromisoformat(
                    change_time_str.replace("Z", "+00:00")
                )
            else:
                change_time = change_time_str
        except (ValueError, TypeError):
            # Skip changes with unparseable timestamps
            continue

        # Compute temporal proximity (Requirement 4.3)
        temporal_proximity = compute_temporal_proximity(
            change_time, incident_time, lookback_seconds
        )

        # Determine resource overlap (Requirement 4.4)
        resource_overlap = check_resource_overlap(
            raw_change.get("affectedResource", ""),
            affected_resources,
        )

        # Compute correlation score (Requirements 4.5, 4.6)
        correlation_score = compute_correlation_score(
            temporal_proximity, resource_overlap
        )

        raw_change["temporalProximity"] = temporal_proximity
        raw_change["resourceOverlap"] = resource_overlap
        raw_change["correlationScore"] = correlation_score

        scored_changes.append(raw_change)

    # Sort by correlation score descending
    scored_changes.sort(key=lambda c: c["correlationScore"], reverse=True)

    # Build response data
    response_data: dict[str, Any] = {
        "changes": scored_changes,
        "lookbackWindow": lookback_window_str,
        "affectedResources": affected_resources,
    }

    # Add partial failure metadata if applicable
    if unavailable_sources:
        response_data["unavailableSources"] = unavailable_sources
        response_data["reducedConfidence"] = True

    # Build message
    if not scored_changes and not unavailable_sources:
        message = f"No changes found in {lookback_window_str} lookback window"
    elif not scored_changes and unavailable_sources:
        message = (
            f"No changes found in available sources. "
            f"Unavailable: {', '.join(unavailable_sources)}"
        )
    elif unavailable_sources:
        message = (
            f"Partial results: found {len(scored_changes)} change(s). "
            f"Unavailable sources: {', '.join(unavailable_sources)}"
        )
    else:
        message = f"Found {len(scored_changes)} correlated change(s) in {lookback_window_str} lookback window"

    return SkillResponse(
        status="success",
        message=message,
        data=response_data,
    ).model_dump(by_alias=True)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point: read CorrelationRequest from stdin, write SkillResponse to stdout."""
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
            result = run_deploy_correlation(request)
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
