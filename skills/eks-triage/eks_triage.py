"""EKS Triage skill — main entry point.

Orchestrates sub-checks for a complete EKS/K8s cluster diagnosis:
- Cluster health (nodes, pods, events)
- Pod failure classification
- Node condition detection
- EKS addon status
- Recent K8s rollout detection (deploy-correlation absorbed)

Input: JSON on stdin with cluster, region, namespace (optional)
Output: SkillResponse JSON on stdout

Usage:
    echo '{"cluster": "my-cluster", "region": "us-east-1"}' | uv run eks_triage.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent))

from skills.shared.mcp_client import kubernetes_mcp
from skills.shared.models import SkillResponse
from skills.eks_triage.cluster_health import check_cluster_health
from skills.eks_triage.pod_failures import triage_pod_failures
from skills.eks_triage.addon_status import check_addon_health


# --- K8s Rollout Detection (absorbed from deploy-correlation) ---

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


def detect_recent_rollouts(
    cluster: str, namespace: str | None, lookback_hours: int = 24
) -> list[dict[str, Any]]:
    """Detect recent K8s rollouts/scaling events within the lookback window."""
    namespaces = [namespace] if namespace else ["default"]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    changes: list[dict[str, Any]] = []

    for ns in namespaces:
        response = kubernetes_mcp.invoke(
            "getEvents",
            {"namespace": ns, "fieldSelector": "type=Normal"},
        )
        if not response.success:
            continue

        events = response.data.get("items", [])
        for event in events:
            reason = event.get("reason", "")
            if reason not in _K8S_CHANGE_REASONS:
                continue

            involved_object = event.get("involvedObject", {})
            event_time_str = event.get("lastTimestamp") or event.get("firstTimestamp", "")
            event_time = _parse_timestamp(event_time_str)

            if event_time < cutoff:
                continue

            changes.append({
                "reason": reason,
                "kind": involved_object.get("kind", ""),
                "name": involved_object.get("name", ""),
                "namespace": involved_object.get("namespace", ns),
                "timestamp": event_time.isoformat(),
                "message": event.get("message", ""),
            })

    return changes


# --- Input Validation ---


def validate_input(data: dict[str, Any]) -> tuple[str, str, str | None]:
    """Validate input fields. Returns (cluster, region, namespace)."""
    cluster = data.get("cluster")
    if not cluster or not isinstance(cluster, str) or not cluster.strip():
        raise ValueError("Missing required field: 'cluster'")

    region = data.get("region")
    if not region or not isinstance(region, str) or not region.strip():
        raise ValueError("Missing required field: 'region'")

    namespace = data.get("namespace")
    if namespace is not None and (not isinstance(namespace, str) or not namespace.strip()):
        namespace = None

    return cluster.strip(), region.strip(), namespace.strip() if namespace else None


# --- Main Triage Logic ---


def run_eks_triage(cluster: str, region: str, namespace: str | None = None) -> SkillResponse:
    """Run the full EKS triage and return a SkillResponse."""

    # 1. Cluster health (nodes, pods, events)
    cluster_health_report = check_cluster_health(cluster, region, namespace)

    # 2. Pod failure classification
    pod_failure_data = triage_pod_failures(cluster, namespace)

    # 3. EKS addon health
    addon_data = check_addon_health(cluster, region)

    # 4. Recent rollouts/changes (absorbed from deploy-correlation)
    recent_rollouts = detect_recent_rollouts(cluster, namespace)

    # Assemble combined report
    report = {
        "cluster": cluster,
        "region": region,
        "clusterHealth": cluster_health_report.model_dump(by_alias=True, mode="json"),
        "podFailures": pod_failure_data,
        "addonHealth": addon_data,
        "recentRollouts": recent_rollouts,
    }

    return SkillResponse(status="success", message="", data=report)


# --- Helpers ---


def _parse_timestamp(ts_str: str) -> datetime:
    if not ts_str:
        return datetime.now(timezone.utc)
    try:
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


# --- Entry Point ---


def main() -> None:
    """Entry point: read JSON from stdin, run EKS triage, write response to stdout."""
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
        cluster, region, namespace = validate_input(data)
        result = run_eks_triage(cluster, region, namespace)
        print(json.dumps(result.model_dump(by_alias=True, mode="json")))

    except json.JSONDecodeError as e:
        response = SkillResponse(status="error", message=f"Invalid JSON input: {e}", data=None)
        print(json.dumps(response.model_dump(by_alias=True, mode="json")))
        sys.exit(1)

    except ValueError as e:
        response = SkillResponse(status="error", message=str(e), data=None)
        print(json.dumps(response.model_dump(by_alias=True, mode="json")))
        sys.exit(1)

    except Exception as e:
        response = SkillResponse(status="error", message=f"Unexpected error: {e}", data=None)
        print(json.dumps(response.model_dump(by_alias=True, mode="json")))
        sys.exit(1)


if __name__ == "__main__":
    main()
