"""Pod Failure Triage sub-module for EKS Triage.

Classifies pod failures cluster-wide by reason (CrashLoopBackOff, OOMKilled,
ImagePullBackOff, CreateContainerError, SandboxError) and reports the namespace
for each failure.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from skills.shared.mcp_client import kubernetes_mcp
from skills.shared.models import PodFailure, PodFailureReason


def classify_pod_failure(pod: dict[str, Any]) -> PodFailure | None:
    """Classify a single pod into a failure reason category.

    Priority order:
    1. OOMKilled (terminated containers with exit code 137 or reason OOMKilled)
    2. CrashLoopBackOff (waiting state with this reason)
    3. ImagePullBackOff (waiting state with this reason or ErrImagePull)
    4. CreateContainerError (waiting state with this reason)
    5. SandboxError (pod-level sandbox failure)
    """
    pod_name = pod.get("metadata", {}).get("name", "unknown")
    namespace = pod.get("metadata", {}).get("namespace", "default")
    status = pod.get("status", {})
    phase = status.get("phase", "")

    if phase in ("Running", "Succeeded"):
        container_statuses = status.get("containerStatuses", [])
        for cs in container_statuses:
            last_state = cs.get("lastState", {})
            terminated = last_state.get("terminated", {})
            if terminated.get("reason") == "OOMKilled" or terminated.get("exitCode") == 137:
                restart_count = cs.get("restartCount", 0)
                if restart_count > 0:
                    return PodFailure(
                        name=pod_name, namespace=namespace,
                        reason=PodFailureReason.OOM_KILLED,
                        restartCount=restart_count,
                        lastTransition=_parse_timestamp(terminated.get("finishedAt", "")),
                        message=terminated.get("message", "Container OOMKilled"),
                    )
            waiting = cs.get("state", {}).get("waiting", {})
            if waiting.get("reason") == "CrashLoopBackOff":
                return PodFailure(
                    name=pod_name, namespace=namespace,
                    reason=PodFailureReason.CRASH_LOOP_BACK_OFF,
                    restartCount=cs.get("restartCount", 0),
                    lastTransition=_parse_timestamp(_get_last_transition_time(status)),
                    message=waiting.get("message", "Container in CrashLoopBackOff"),
                )
        return None

    container_statuses = status.get("containerStatuses", [])
    init_container_statuses = status.get("initContainerStatuses", [])
    all_containers = container_statuses + init_container_statuses

    # Priority 1: OOMKilled
    for cs in all_containers:
        terminated = cs.get("state", {}).get("terminated", {})
        if terminated.get("reason") == "OOMKilled" or terminated.get("exitCode") == 137:
            return PodFailure(
                name=pod_name, namespace=namespace,
                reason=PodFailureReason.OOM_KILLED,
                restartCount=cs.get("restartCount", 0),
                lastTransition=_parse_timestamp(terminated.get("finishedAt", "")),
                message=terminated.get("message", "Container OOMKilled"),
            )
        last_terminated = cs.get("lastState", {}).get("terminated", {})
        if last_terminated.get("reason") == "OOMKilled" or last_terminated.get("exitCode") == 137:
            return PodFailure(
                name=pod_name, namespace=namespace,
                reason=PodFailureReason.OOM_KILLED,
                restartCount=cs.get("restartCount", 0),
                lastTransition=_parse_timestamp(last_terminated.get("finishedAt", "")),
                message=last_terminated.get("message", "Container OOMKilled"),
            )

    # Priority 2: CrashLoopBackOff
    for cs in all_containers:
        waiting = cs.get("state", {}).get("waiting", {})
        if waiting.get("reason") == "CrashLoopBackOff":
            return PodFailure(
                name=pod_name, namespace=namespace,
                reason=PodFailureReason.CRASH_LOOP_BACK_OFF,
                restartCount=cs.get("restartCount", 0),
                lastTransition=_parse_timestamp(_get_last_transition_time(status)),
                message=waiting.get("message", "Container in CrashLoopBackOff"),
            )

    # Priority 3: ImagePullBackOff
    for cs in all_containers:
        waiting = cs.get("state", {}).get("waiting", {})
        if waiting.get("reason") in ("ImagePullBackOff", "ErrImagePull"):
            return PodFailure(
                name=pod_name, namespace=namespace,
                reason=PodFailureReason.IMAGE_PULL_BACK_OFF,
                restartCount=cs.get("restartCount", 0),
                lastTransition=_parse_timestamp(_get_last_transition_time(status)),
                message=waiting.get("message", "Failed to pull image"),
            )

    # Priority 4: CreateContainerError
    for cs in all_containers:
        waiting = cs.get("state", {}).get("waiting", {})
        if waiting.get("reason") == "CreateContainerError":
            return PodFailure(
                name=pod_name, namespace=namespace,
                reason=PodFailureReason.CREATE_CONTAINER_ERROR,
                restartCount=cs.get("restartCount", 0),
                lastTransition=_parse_timestamp(_get_last_transition_time(status)),
                message=waiting.get("message", "Failed to create container"),
            )

    # Priority 5: SandboxError
    conditions = status.get("conditions", [])
    for condition in conditions:
        if condition.get("reason", "").lower().find("sandbox") != -1:
            return PodFailure(
                name=pod_name, namespace=namespace,
                reason=PodFailureReason.SANDBOX_ERROR,
                restartCount=0,
                lastTransition=_parse_timestamp(condition.get("lastTransitionTime", "")),
                message=condition.get("message", "Sandbox creation failed"),
            )

    if "sandbox" in status.get("message", "").lower():
        return PodFailure(
            name=pod_name, namespace=namespace,
            reason=PodFailureReason.SANDBOX_ERROR,
            restartCount=0,
            lastTransition=_parse_timestamp(_get_last_transition_time(status)),
            message=status.get("message", "Sandbox creation failed"),
        )

    # Generic non-zero exit code
    for cs in all_containers:
        terminated = cs.get("state", {}).get("terminated", {})
        if terminated and terminated.get("exitCode", 0) != 0:
            return PodFailure(
                name=pod_name, namespace=namespace,
                reason=PodFailureReason.CRASH_LOOP_BACK_OFF,
                restartCount=cs.get("restartCount", 0),
                lastTransition=_parse_timestamp(terminated.get("finishedAt", "")),
                message=terminated.get("message", f"Container exited with code {terminated.get('exitCode')}"),
            )

    return None


def triage_pod_failures(
    cluster: str, namespace: str | None = None, label_selector: str | None = None
) -> dict[str, Any]:
    """Classify pod failures and return structured results."""
    args: dict[str, Any] = {"namespace": namespace or ""}
    if label_selector:
        args["labelSelector"] = label_selector

    response = kubernetes_mcp.invoke("getPods", args)
    if not response.success:
        return {"error": f"Failed to fetch pods from cluster '{cluster}': {response.error}"}

    pods = response.data.get("pods", [])
    if not isinstance(pods, list):
        pods = []

    failures: list[PodFailure] = []
    for pod in pods:
        failure = classify_pod_failure(pod)
        if failure is not None:
            failures.append(failure)

    summary_by_reason: dict[str, int] = {
        PodFailureReason.CRASH_LOOP_BACK_OFF.value: 0,
        PodFailureReason.OOM_KILLED.value: 0,
        PodFailureReason.IMAGE_PULL_BACK_OFF.value: 0,
        PodFailureReason.CREATE_CONTAINER_ERROR.value: 0,
        PodFailureReason.SANDBOX_ERROR.value: 0,
    }
    affected_namespaces: set[str] = set()

    for f in failures:
        summary_by_reason[f.reason.value] += 1
        affected_namespaces.add(f.namespace)

    return {
        "cluster": cluster,
        "total_failed_pods": len(failures),
        "failures": [
            {
                "name": f.name, "namespace": f.namespace, "reason": f.reason.value,
                "restartCount": f.restart_count,
                "lastTransition": f.last_transition.isoformat(), "message": f.message,
            }
            for f in failures
        ],
        "summary_by_reason": summary_by_reason,
        "affected_namespaces": sorted(affected_namespaces),
    }


# --- Helpers ---


def _parse_timestamp(ts: str) -> datetime:
    if not ts:
        return datetime.now(timezone.utc)
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


def _get_last_transition_time(status: dict[str, Any]) -> str:
    conditions = status.get("conditions", [])
    if not conditions:
        return ""
    times = [c.get("lastTransitionTime", "") for c in conditions if c.get("lastTransitionTime")]
    return max(times) if times else ""
