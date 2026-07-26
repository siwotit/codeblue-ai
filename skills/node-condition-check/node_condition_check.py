"""Node Condition Check skill script for CodeBlue AI.

Detects unhealthy node conditions in a Kubernetes cluster:
- NotReady nodes (kubelet failure, network partition)
- MemoryPressure (memory exhaustion, pod eviction risk)
- DiskPressure (disk space exhaustion)
- PIDPressure (process ID exhaustion)
- NetworkUnavailable (node network failure)

Classification: Mixed (balanced script + prompt guidance)
Requirements: 3.3, 14.2

Usage:
    echo '{"cluster": "my-cluster", "region": "us-east-1"}' | uv run node_condition_check.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

# Add parent paths so shared modules are importable
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent))

from skills.shared.mcp_client import kubernetes_mcp
from skills.shared.models import (
    NodeCondition,
    NodeConditionType,
    SkillResponse,
)


# --- Constants ---

# All condition types this skill detects
CONDITION_MAP: dict[str, NodeConditionType] = {
    "MemoryPressure": NodeConditionType.MEMORY_PRESSURE,
    "DiskPressure": NodeConditionType.DISK_PRESSURE,
    "PIDPressure": NodeConditionType.PID_PRESSURE,
    "NetworkUnavailable": NodeConditionType.NETWORK_UNAVAILABLE,
}


# --- Input Validation ---


def validate_input(data: dict[str, Any]) -> tuple[str, str]:
    """Validate and extract input fields.

    Returns:
        Tuple of (cluster, region).

    Raises:
        ValueError: If required fields are missing.
    """
    cluster = data.get("cluster")
    if not cluster or not isinstance(cluster, str) or not cluster.strip():
        raise ValueError("Missing required field: 'cluster'")

    region = data.get("region")
    if not region or not isinstance(region, str) or not region.strip():
        raise ValueError("Missing required field: 'region'")

    return cluster.strip(), region.strip()


# --- Data Fetching ---


def fetch_nodes(cluster: str) -> list[dict[str, Any]]:
    """Fetch node list from the kubernetes-mcp.

    Returns list of node objects with status information.
    """
    response = kubernetes_mcp.invoke(
        "list_nodes",
        {"cluster": cluster},
    )
    if response.success and response.data:
        return response.data.get("items", [])
    return []


# --- Processing ---


def process_node_conditions(nodes: list[dict[str, Any]]) -> list[NodeCondition]:
    """Process raw node data and extract all unhealthy conditions.

    Detects:
    - NotReady: node's Ready condition is not True
    - MemoryPressure: condition status is True
    - DiskPressure: condition status is True
    - PIDPressure: condition status is True
    - NetworkUnavailable: condition status is True

    Returns list of NodeCondition for all detected problems.
    """
    conditions: list[NodeCondition] = []

    for node in nodes:
        node_name = node.get("metadata", {}).get("name", "unknown")
        node_conditions = node.get("status", {}).get("conditions", [])

        for cond in node_conditions:
            cond_type = cond.get("type", "")
            cond_status = cond.get("status", "False")

            # Detect NotReady: Ready condition with status != True
            if cond_type == "Ready" and cond_status != "True":
                conditions.append(
                    NodeCondition(
                        nodeName=node_name,
                        condition=NodeConditionType.NOT_READY,
                        status=True,
                        since=_parse_timestamp(
                            cond.get("lastTransitionTime", "")
                        ),
                        message=cond.get("message", ""),
                    )
                )

            # Detect pressure conditions: status == True means pressure is active
            if cond_type in CONDITION_MAP and cond_status == "True":
                conditions.append(
                    NodeCondition(
                        nodeName=node_name,
                        condition=CONDITION_MAP[cond_type],
                        status=True,
                        since=_parse_timestamp(
                            cond.get("lastTransitionTime", "")
                        ),
                        message=cond.get("message", ""),
                    )
                )

    return conditions


def build_summary(conditions: list[NodeCondition]) -> dict[str, int]:
    """Build a summary count of each condition type.

    Returns dict with counts for each of the 5 condition types.
    """
    summary = {
        "notReadyCount": 0,
        "memoryPressureCount": 0,
        "diskPressureCount": 0,
        "pidPressureCount": 0,
        "networkUnavailableCount": 0,
    }

    for cond in conditions:
        if cond.condition == NodeConditionType.NOT_READY:
            summary["notReadyCount"] += 1
        elif cond.condition == NodeConditionType.MEMORY_PRESSURE:
            summary["memoryPressureCount"] += 1
        elif cond.condition == NodeConditionType.DISK_PRESSURE:
            summary["diskPressureCount"] += 1
        elif cond.condition == NodeConditionType.PID_PRESSURE:
            summary["pidPressureCount"] += 1
        elif cond.condition == NodeConditionType.NETWORK_UNAVAILABLE:
            summary["networkUnavailableCount"] += 1

    return summary


# --- Helpers ---


def _parse_timestamp(ts_str: str) -> datetime:
    """Parse an ISO 8601 timestamp string.

    Returns current UTC time if parsing fails.
    """
    if not ts_str:
        return datetime.now(timezone.utc)
    try:
        # Handle Kubernetes timestamp format (e.g., 2024-01-15T10:30:00Z)
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


# --- Main Logic ---


def check_node_conditions(cluster: str, region: str) -> SkillResponse:
    """Run the node condition check and return a SkillResponse.

    Fetches nodes from the kubernetes-mcp, processes conditions,
    and returns a structured response with all detected problems.
    """
    # Fetch data from kubernetes-mcp
    raw_nodes = fetch_nodes(cluster)

    # Process conditions
    conditions = process_node_conditions(raw_nodes)

    # Calculate node counts
    total_nodes = len(raw_nodes)
    # A node is healthy if it has no conditions in our detected list
    nodes_with_conditions = {c.node_name for c in conditions}
    healthy_nodes = total_nodes - len(nodes_with_conditions)

    # Build condition summary
    summary = build_summary(conditions)

    # Serialize conditions for output
    conditions_data = [
        cond.model_dump(by_alias=True, mode="json") for cond in conditions
    ]

    return SkillResponse(
        status="success",
        message="",
        data={
            "clusterName": cluster,
            "region": region,
            "totalNodes": total_nodes,
            "healthyNodes": healthy_nodes,
            "conditions": conditions_data,
            "summary": summary,
        },
    )


def main() -> None:
    """Entry point: read JSON from stdin, run check, write response to stdout."""
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            response = SkillResponse(
                status="error",
                message="No input provided. Expected JSON with 'cluster' and 'region' fields.",
                data=None,
            )
            print(json.dumps(response.model_dump(by_alias=True, mode="json")))
            sys.exit(1)

        data = json.loads(raw_input)
        cluster, region = validate_input(data)
        result = check_node_conditions(cluster, region)
        print(json.dumps(result.model_dump(by_alias=True, mode="json")))

    except json.JSONDecodeError as e:
        response = SkillResponse(
            status="error",
            message=f"Invalid JSON input: {e}",
            data=None,
        )
        print(json.dumps(response.model_dump(by_alias=True, mode="json")))
        sys.exit(1)

    except ValueError as e:
        response = SkillResponse(
            status="error",
            message=str(e),
            data=None,
        )
        print(json.dumps(response.model_dump(by_alias=True, mode="json")))
        sys.exit(1)

    except Exception as e:
        response = SkillResponse(
            status="error",
            message=f"Unexpected error during node condition check: {e}",
            data=None,
        )
        print(json.dumps(response.model_dump(by_alias=True, mode="json")))
        sys.exit(1)


if __name__ == "__main__":
    main()
