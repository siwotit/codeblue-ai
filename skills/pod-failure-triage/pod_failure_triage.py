"""Pod Failure Triage skill script for CodeBlue AI.

Classifies pod failures cluster-wide by reason (CrashLoopBackOff, OOMKilled,
ImagePullBackOff, CreateContainerError, SandboxError) and reports the namespace
for each failure.

Accepts JSON input via stdin, returns SkillResponse JSON to stdout.
Uses kubernetes-mcp to fetch pod statuses.

Requirements: 3.4, 14.2
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

# Add parent paths for shared imports
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from shared.mcp_client import kubernetes_mcp
from shared.models import PodFailure, PodFailureReason, SkillResponse


def classify_pod_failure(pod: dict[str, Any]) -> PodFailure | None:
    """Classify a single pod into a failure reason category.

    Each pod is classified into exactly one reason based on container
    status inspection. Classification priority:
    1. OOMKilled (terminated containers with exit code 137 or reason OOMKilled)
    2. CrashLoopBackOff (waiting state with this reason)
    3. ImagePullBackOff (waiting state with this reason or ErrImagePull)
    4. CreateContainerError (waiting state with this reason)
    5. SandboxError (pod-level sandbox failure)

    Args:
        pod: Raw pod data from kubernetes-mcp.

    Returns:
        PodFailure if the pod is in a failure state, None otherwise.
    """
    pod_name = pod.get("metadata", {}).get("name", "unknown")
    namespace = pod.get("metadata", {}).get("namespace", "default")
    status = pod.get("status", {})
    phase = status.get("phase", "")

    # Skip pods that are running or succeeded
    if phase in ("Running", "Succeeded"):
        # Still check for OOMKilled in recently restarted containers
        container_statuses = status.get("containerStatuses", [])
        for cs in container_statuses:
            last_state = cs.get("lastState", {})
            terminated = last_state.get("terminated", {})
            if terminated.get("reason") == "OOMKilled" or terminated.get("exitCode") == 137:
                restart_count = cs.get("restartCount", 0)
                if restart_count > 0:
                    return PodFailure(
                        name=pod_name,
                        namespace=namespace,
                        reason=PodFailureReason.OOM_KILLED,
                        restartCount=restart_count,
                        lastTransition=_parse_timestamp(
                            terminated.get("finishedAt", "")
                        ),
                        message=terminated.get("message", "Container OOMKilled"),
                    )
        # Check for CrashLoopBackOff in running pods (waiting containers)
        for cs in container_statuses:
            waiting = cs.get("state", {}).get("waiting", {})
            if waiting.get("reason") == "CrashLoopBackOff":
                return PodFailure(
                    name=pod_name,
                    namespace=namespace,
                    reason=PodFailureReason.CRASH_LOOP_BACK_OFF,
                    restartCount=cs.get("restartCount", 0),
                    lastTransition=_parse_timestamp(
                        _get_last_transition_time(status)
                    ),
                    message=waiting.get("message", "Container in CrashLoopBackOff"),
                )
        return None

    container_statuses = status.get("containerStatuses", [])
    init_container_statuses = status.get("initContainerStatuses", [])
    all_containers = container_statuses + init_container_statuses

    # Priority 1: Check for OOMKilled
    for cs in all_containers:
        terminated = cs.get("state", {}).get("terminated", {})
        if terminated.get("reason") == "OOMKilled" or terminated.get("exitCode") == 137:
            return PodFailure(
                name=pod_name,
                namespace=namespace,
                reason=PodFailureReason.OOM_KILLED,
                restartCount=cs.get("restartCount", 0),
                lastTransition=_parse_timestamp(
                    terminated.get("finishedAt", "")
                ),
                message=terminated.get("message", "Container OOMKilled"),
            )
        # Also check lastState for OOMKilled
        last_terminated = cs.get("lastState", {}).get("terminated", {})
        if last_terminated.get("reason") == "OOMKilled" or last_terminated.get("exitCode") == 137:
            return PodFailure(
                name=pod_name,
                namespace=namespace,
                reason=PodFailureReason.OOM_KILLED,
                restartCount=cs.get("restartCount", 0),
                lastTransition=_parse_timestamp(
                    last_terminated.get("finishedAt", "")
                ),
                message=last_terminated.get("message", "Container OOMKilled"),
            )

    # Priority 2: Check for CrashLoopBackOff
    for cs in all_containers:
        waiting = cs.get("state", {}).get("waiting", {})
        if waiting.get("reason") == "CrashLoopBackOff":
            return PodFailure(
                name=pod_name,
                namespace=namespace,
                reason=PodFailureReason.CRASH_LOOP_BACK_OFF,
                restartCount=cs.get("restartCount", 0),
                lastTransition=_parse_timestamp(
                    _get_last_transition_time(status)
                ),
                message=waiting.get("message", "Container in CrashLoopBackOff"),
            )

    # Priority 3: Check for ImagePullBackOff
    for cs in all_containers:
        waiting = cs.get("state", {}).get("waiting", {})
        if waiting.get("reason") in ("ImagePullBackOff", "ErrImagePull"):
            return PodFailure(
                name=pod_name,
                namespace=namespace,
                reason=PodFailureReason.IMAGE_PULL_BACK_OFF,
                restartCount=cs.get("restartCount", 0),
                lastTransition=_parse_timestamp(
                    _get_last_transition_time(status)
                ),
                message=waiting.get("message", "Failed to pull image"),
            )

    # Priority 4: Check for CreateContainerError
    for cs in all_containers:
        waiting = cs.get("state", {}).get("waiting", {})
        if waiting.get("reason") == "CreateContainerError":
            return PodFailure(
                name=pod_name,
                namespace=namespace,
                reason=PodFailureReason.CREATE_CONTAINER_ERROR,
                restartCount=cs.get("restartCount", 0),
                lastTransition=_parse_timestamp(
                    _get_last_transition_time(status)
                ),
                message=waiting.get("message", "Failed to create container"),
            )

    # Priority 5: Check for SandboxError (pod-level conditions)
    conditions = status.get("conditions", [])
    for condition in conditions:
        if condition.get("reason", "").lower().find("sandbox") != -1:
            return PodFailure(
                name=pod_name,
                namespace=namespace,
                reason=PodFailureReason.SANDBOX_ERROR,
                restartCount=0,
                lastTransition=_parse_timestamp(
                    condition.get("lastTransitionTime", "")
                ),
                message=condition.get("message", "Sandbox creation failed"),
            )

    # Check pod message for sandbox errors
    if "sandbox" in status.get("message", "").lower():
        return PodFailure(
            name=pod_name,
            namespace=namespace,
            reason=PodFailureReason.SANDBOX_ERROR,
            restartCount=0,
            lastTransition=_parse_timestamp(
                _get_last_transition_time(status)
            ),
            message=status.get("message", "Sandbox creation failed"),
        )

    # Pod is in a failed/pending state but doesn't match known patterns
    # Check for generic terminated containers with non-zero exit codes
    for cs in all_containers:
        terminated = cs.get("state", {}).get("terminated", {})
        if terminated and terminated.get("exitCode", 0) != 0:
            return PodFailure(
                name=pod_name,
                namespace=namespace,
                reason=PodFailureReason.CRASH_LOOP_BACK_OFF,
                restartCount=cs.get("restartCount", 0),
                lastTransition=_parse_timestamp(
                    terminated.get("finishedAt", "")
                ),
                message=terminated.get(
                    "message",
                    f"Container exited with code {terminated.get('exitCode')}",
                ),
            )

    return None


def _parse_timestamp(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp string, returning current time if empty/invalid."""
    if not ts:
        return datetime.now(timezone.utc)
    try:
        # Handle Kubernetes timestamp format (e.g., 2024-01-15T10:30:00Z)
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


def _get_last_transition_time(status: dict[str, Any]) -> str:
    """Extract the most recent transition time from pod conditions."""
    conditions = status.get("conditions", [])
    if not conditions:
        return ""
    # Return the latest lastTransitionTime
    times = [c.get("lastTransitionTime", "") for c in conditions if c.get("lastTransitionTime")]
    if times:
        return max(times)
    return ""


def triage_pod_failures(
    cluster: str,
    namespace: str | None = None,
    label_selector: str | None = None,
) -> SkillResponse:
    """Classify pod failures by reason and report namespace for each.

    Fetches pod data via kubernetes-mcp and classifies each failed pod
    into exactly one PodFailureReason category.

    Args:
        cluster: EKS cluster name.
        namespace: Optional namespace to scope the investigation.
        label_selector: Optional label selector to filter pods.

    Returns:
        SkillResponse with classified pod failures.
    """
    # Fetch pods via kubernetes-mcp
    args: dict[str, Any] = {}
    if namespace:
        args["namespace"] = namespace
    else:
        args["namespace"] = ""  # cluster-wide
    if label_selector:
        args["labelSelector"] = label_selector

    response = kubernetes_mcp.invoke("getPods", args)

    if not response.success:
        return SkillResponse(
            status="error",
            message=f"Failed to fetch pods from cluster '{cluster}': {response.error}",
            data=None,
        )

    pods = response.data.get("pods", [])
    if not isinstance(pods, list):
        pods = []

    # Classify each pod
    failures: list[PodFailure] = []
    for pod in pods:
        failure = classify_pod_failure(pod)
        if failure is not None:
            failures.append(failure)

    # Build summary by reason
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

    # Serialize failures
    failure_dicts = [
        {
            "name": f.name,
            "namespace": f.namespace,
            "reason": f.reason.value,
            "restartCount": f.restart_count,
            "lastTransition": f.last_transition.isoformat(),
            "message": f.message,
        }
        for f in failures
    ]

    return SkillResponse(
        status="success",
        message="",
        data={
            "cluster": cluster,
            "total_failed_pods": len(failures),
            "failures": failure_dicts,
            "summary_by_reason": summary_by_reason,
            "affected_namespaces": sorted(affected_namespaces),
        },
    )


def main() -> None:
    """Entry point: read JSON from stdin, run triage, write response to stdout."""
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            result = SkillResponse(
                status="error",
                message="No input provided. Expected JSON with 'cluster' field.",
                data=None,
            )
            print(result.model_dump_json())
            return

        input_data = json.loads(raw_input)
    except json.JSONDecodeError as e:
        result = SkillResponse(
            status="error",
            message=f"Invalid JSON input: {e}",
            data=None,
        )
        print(result.model_dump_json())
        return

    cluster = input_data.get("cluster")
    if not cluster:
        result = SkillResponse(
            status="error",
            message="Missing required field 'cluster' in input.",
            data=None,
        )
        print(result.model_dump_json())
        return

    namespace = input_data.get("namespace")
    label_selector = input_data.get("label_selector")

    result = triage_pod_failures(
        cluster=cluster,
        namespace=namespace,
        label_selector=label_selector,
    )
    print(result.model_dump_json())


if __name__ == "__main__":
    main()
