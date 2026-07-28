"""Cluster Health sub-module for EKS Triage.

Checks overall Kubernetes cluster state including:
- Total/ready/not-ready nodes
- Pending pods and pod failures
- Resource pressure conditions (MemoryPressure, DiskPressure, PIDPressure, NetworkUnavailable)
- Recent warning/error events (max 100, last 1 hour)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from skills.shared.mcp_client import kubernetes_mcp
from skills.shared.models import (
    ClusterHealth,
    ClusterHealthReport,
    K8sEventSummary,
    K8sEventType,
    NodeCondition,
    NodeConditionType,
    NodesStatus,
    PodFailure,
    PodFailureReason,
    PodsStatus,
)


# --- Constants ---

MAX_EVENTS = 100
EVENT_WINDOW_HOURS = 1

CRITICAL_POD_FAILURE_RATIO = 0.25
DEGRADED_POD_FAILURE_RATIO = 0.10

PRESSURE_CONDITIONS = {
    "MemoryPressure": NodeConditionType.MEMORY_PRESSURE,
    "DiskPressure": NodeConditionType.DISK_PRESSURE,
    "PIDPressure": NodeConditionType.PID_PRESSURE,
    "NetworkUnavailable": NodeConditionType.NETWORK_UNAVAILABLE,
}


# --- Data Fetching ---


def fetch_nodes(cluster: str) -> list[dict[str, Any]]:
    """Fetch node list from kubernetes-mcp."""
    response = kubernetes_mcp.invoke("list_nodes", {"cluster": cluster})
    if response.success and response.data:
        return response.data.get("items", [])
    return []


def fetch_pods(cluster: str, namespace: str | None = None) -> list[dict[str, Any]]:
    """Fetch pod list from kubernetes-mcp."""
    arguments: dict[str, Any] = {"cluster": cluster}
    if namespace:
        arguments["namespace"] = namespace
    response = kubernetes_mcp.invoke("list_pods", arguments)
    if response.success and response.data:
        return response.data.get("items", [])
    return []


def fetch_events(cluster: str, namespace: str | None = None) -> list[dict[str, Any]]:
    """Fetch recent events from kubernetes-mcp."""
    arguments: dict[str, Any] = {"cluster": cluster}
    if namespace:
        arguments["namespace"] = namespace
    response = kubernetes_mcp.invoke("list_events", arguments)
    if response.success and response.data:
        return response.data.get("items", [])
    return []


# --- Processing ---


def process_nodes(nodes: list[dict[str, Any]]) -> NodesStatus:
    """Process raw node data into NodesStatus."""
    total = len(nodes)
    ready = 0
    not_ready_names: list[str] = []
    conditions: list[NodeCondition] = []

    for node in nodes:
        node_name = node.get("metadata", {}).get("name", "unknown")
        node_conditions = node.get("status", {}).get("conditions", [])
        is_ready = False

        for cond in node_conditions:
            cond_type = cond.get("type", "")
            cond_status = cond.get("status", "False")

            if cond_type == "Ready":
                if cond_status == "True":
                    is_ready = True
                else:
                    not_ready_names.append(node_name)
                    conditions.append(NodeCondition(
                        nodeName=node_name,
                        condition=NodeConditionType.NOT_READY,
                        status=True,
                        since=_parse_timestamp(cond.get("lastTransitionTime", "")),
                        message=cond.get("message", ""),
                    ))

            if cond_type in PRESSURE_CONDITIONS and cond_status == "True":
                conditions.append(NodeCondition(
                    nodeName=node_name,
                    condition=PRESSURE_CONDITIONS[cond_type],
                    status=True,
                    since=_parse_timestamp(cond.get("lastTransitionTime", "")),
                    message=cond.get("message", ""),
                ))

        if is_ready:
            ready += 1

    return NodesStatus(total=total, ready=ready, notReady=not_ready_names, conditions=conditions)


def process_pods(pods: list[dict[str, Any]]) -> PodsStatus:
    """Process raw pod data into PodsStatus."""
    total = len(pods)
    running = 0
    pending = 0
    failed = 0
    crash_looping: list[PodFailure] = []
    oom_killed: list[PodFailure] = []

    for pod in pods:
        metadata = pod.get("metadata", {})
        pod_name = metadata.get("name", "unknown")
        pod_namespace = metadata.get("namespace", "default")
        status = pod.get("status", {})
        phase = status.get("phase", "Unknown")

        if phase == "Running":
            running += 1
        elif phase == "Pending":
            pending += 1
        elif phase in ("Failed", "Unknown"):
            failed += 1

        container_statuses = status.get("containerStatuses", [])
        for cs in container_statuses:
            restart_count = cs.get("restartCount", 0)
            state = cs.get("state", {})
            last_state = cs.get("lastState", {})

            waiting = state.get("waiting", {})
            waiting_reason = waiting.get("reason", "")

            if waiting_reason == "CrashLoopBackOff":
                crash_looping.append(PodFailure(
                    name=pod_name, namespace=pod_namespace,
                    reason=PodFailureReason.CRASH_LOOP_BACK_OFF,
                    restartCount=restart_count,
                    lastTransition=_parse_timestamp(_get_last_transition(status)),
                    message=waiting.get("message", "Container in CrashLoopBackOff"),
                ))
            elif waiting_reason == "ImagePullBackOff":
                crash_looping.append(PodFailure(
                    name=pod_name, namespace=pod_namespace,
                    reason=PodFailureReason.IMAGE_PULL_BACK_OFF,
                    restartCount=restart_count,
                    lastTransition=_parse_timestamp(_get_last_transition(status)),
                    message=waiting.get("message", "Image pull back off"),
                ))
            elif waiting_reason == "CreateContainerError":
                crash_looping.append(PodFailure(
                    name=pod_name, namespace=pod_namespace,
                    reason=PodFailureReason.CREATE_CONTAINER_ERROR,
                    restartCount=restart_count,
                    lastTransition=_parse_timestamp(_get_last_transition(status)),
                    message=waiting.get("message", "Create container error"),
                ))
            elif waiting_reason == "SandboxError":
                crash_looping.append(PodFailure(
                    name=pod_name, namespace=pod_namespace,
                    reason=PodFailureReason.SANDBOX_ERROR,
                    restartCount=restart_count,
                    lastTransition=_parse_timestamp(_get_last_transition(status)),
                    message=waiting.get("message", "Sandbox error"),
                ))

            terminated = last_state.get("terminated", {})
            if terminated.get("reason") == "OOMKilled":
                oom_killed.append(PodFailure(
                    name=pod_name, namespace=pod_namespace,
                    reason=PodFailureReason.OOM_KILLED,
                    restartCount=restart_count,
                    lastTransition=_parse_timestamp(terminated.get("finishedAt", "")),
                    message=terminated.get("message", "Container OOMKilled"),
                ))

    return PodsStatus(
        total=total, running=running, pending=pending, failed=failed,
        crashLooping=crash_looping, oomKilled=oom_killed,
    )


def process_events(events: list[dict[str, Any]], cutoff: datetime) -> list[K8sEventSummary]:
    """Process raw events into K8sEventSummary list (Warning events only, last 1 hour)."""
    filtered: list[K8sEventSummary] = []

    for event in events:
        event_type = event.get("type", "Normal")
        if event_type != "Warning":
            continue

        last_ts_str = event.get("lastTimestamp") or event.get("metadata", {}).get("creationTimestamp", "")
        last_ts = _parse_timestamp(last_ts_str)
        if last_ts < cutoff:
            continue

        first_ts_str = event.get("firstTimestamp") or last_ts_str
        involved_obj = event.get("involvedObject", {})
        obj_ref = f"{involved_obj.get('kind', 'Unknown')}/{involved_obj.get('namespace', '')}/{involved_obj.get('name', '')}"

        filtered.append(K8sEventSummary(
            type=K8sEventType.WARNING,
            reason=event.get("reason", "Unknown"),
            involvedObject=obj_ref,
            message=event.get("message", ""),
            count=event.get("count", 1),
            firstTimestamp=_parse_timestamp(first_ts_str),
            lastTimestamp=last_ts,
        ))

        if len(filtered) >= MAX_EVENTS:
            break

    return filtered


# --- Health Classification ---


def classify_health(nodes: NodesStatus, pods: PodsStatus) -> ClusterHealth:
    """Determine overall cluster health status."""
    if nodes.not_ready:
        return ClusterHealth.CRITICAL

    if pods.total > 0:
        failure_ratio = (pods.pending + pods.failed) / pods.total
        if failure_ratio > CRITICAL_POD_FAILURE_RATIO:
            return ClusterHealth.CRITICAL

    has_pressure = any(c.condition != NodeConditionType.NOT_READY for c in nodes.conditions)
    if has_pressure:
        return ClusterHealth.DEGRADED

    if pods.total > 0:
        failure_ratio = (pods.pending + pods.failed) / pods.total
        if failure_ratio > DEGRADED_POD_FAILURE_RATIO:
            return ClusterHealth.DEGRADED

    if pods.crash_looping or pods.oom_killed:
        return ClusterHealth.DEGRADED

    return ClusterHealth.HEALTHY


# --- Helpers ---


def _parse_timestamp(ts_str: str) -> datetime:
    """Parse ISO 8601 timestamp, returning current UTC on failure."""
    if not ts_str:
        return datetime.now(timezone.utc)
    try:
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


def _get_last_transition(status: dict[str, Any]) -> str:
    """Get the last transition time from pod status conditions."""
    conditions = status.get("conditions", [])
    if conditions:
        return conditions[-1].get("lastTransitionTime", "")
    return ""


# --- Public API ---


def check_cluster_health(
    cluster: str, region: str, namespace: str | None = None
) -> ClusterHealthReport:
    """Run the cluster health check and return a ClusterHealthReport."""
    raw_nodes = fetch_nodes(cluster)
    raw_pods = fetch_pods(cluster, namespace)
    raw_events = fetch_events(cluster, namespace)

    nodes_status = process_nodes(raw_nodes)
    pods_status = process_pods(raw_pods)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=EVENT_WINDOW_HOURS)
    recent_events = process_events(raw_events, cutoff)

    overall_health = classify_health(nodes_status, pods_status)

    return ClusterHealthReport(
        clusterName=cluster,
        region=region,
        overallHealth=overall_health,
        nodes=nodes_status,
        pods=pods_status,
        recentEvents=recent_events,
    )
