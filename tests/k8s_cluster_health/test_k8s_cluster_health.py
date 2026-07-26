"""Unit tests for the k8s-cluster-health skill."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Ensure the project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from skills.shared.models import (
    ClusterHealth,
    NodeConditionType,
    NodesStatus,
    PodFailureReason,
    PodsStatus,
)

# Import the skill functions directly
sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent.parent / "skills" / "k8s-cluster-health"),
)
from k8s_cluster_health import (
    classify_health,
    process_events,
    process_nodes,
    process_pods,
    validate_input,
)


# --- validate_input tests ---


class TestValidateInput:
    def test_valid_input(self):
        data = {"cluster": "my-cluster", "region": "us-east-1"}
        cluster, region, namespace = validate_input(data)
        assert cluster == "my-cluster"
        assert region == "us-east-1"
        assert namespace is None

    def test_valid_input_with_namespace(self):
        data = {"cluster": "my-cluster", "region": "us-east-1", "namespace": "payments"}
        cluster, region, namespace = validate_input(data)
        assert cluster == "my-cluster"
        assert region == "us-east-1"
        assert namespace == "payments"

    def test_missing_cluster(self):
        with pytest.raises(ValueError, match="cluster"):
            validate_input({"region": "us-east-1"})

    def test_empty_cluster(self):
        with pytest.raises(ValueError, match="cluster"):
            validate_input({"cluster": "", "region": "us-east-1"})

    def test_missing_region(self):
        with pytest.raises(ValueError, match="region"):
            validate_input({"cluster": "my-cluster"})

    def test_empty_region(self):
        with pytest.raises(ValueError, match="region"):
            validate_input({"cluster": "my-cluster", "region": "  "})

    def test_whitespace_trimmed(self):
        data = {"cluster": "  my-cluster  ", "region": " us-east-1 "}
        cluster, region, namespace = validate_input(data)
        assert cluster == "my-cluster"
        assert region == "us-east-1"


# --- process_nodes tests ---


class TestProcessNodes:
    def test_empty_nodes(self):
        result = process_nodes([])
        assert result.total == 0
        assert result.ready == 0
        assert result.not_ready == []
        assert result.conditions == []

    def test_all_ready_nodes(self):
        nodes = [
            {
                "metadata": {"name": "node-1"},
                "status": {
                    "conditions": [
                        {"type": "Ready", "status": "True", "lastTransitionTime": "2024-01-01T00:00:00Z"}
                    ]
                },
            },
            {
                "metadata": {"name": "node-2"},
                "status": {
                    "conditions": [
                        {"type": "Ready", "status": "True", "lastTransitionTime": "2024-01-01T00:00:00Z"}
                    ]
                },
            },
        ]
        result = process_nodes(nodes)
        assert result.total == 2
        assert result.ready == 2
        assert result.not_ready == []

    def test_not_ready_node(self):
        nodes = [
            {
                "metadata": {"name": "node-1"},
                "status": {
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "False",
                            "lastTransitionTime": "2024-01-01T10:00:00Z",
                            "message": "kubelet stopped",
                        }
                    ]
                },
            },
        ]
        result = process_nodes(nodes)
        assert result.total == 1
        assert result.ready == 0
        assert result.not_ready == ["node-1"]
        assert len(result.conditions) == 1
        assert result.conditions[0].condition == NodeConditionType.NOT_READY

    def test_memory_pressure(self):
        nodes = [
            {
                "metadata": {"name": "node-1"},
                "status": {
                    "conditions": [
                        {"type": "Ready", "status": "True", "lastTransitionTime": "2024-01-01T00:00:00Z"},
                        {
                            "type": "MemoryPressure",
                            "status": "True",
                            "lastTransitionTime": "2024-01-01T09:00:00Z",
                            "message": "memory low",
                        },
                    ]
                },
            },
        ]
        result = process_nodes(nodes)
        assert result.total == 1
        assert result.ready == 1
        assert len(result.conditions) == 1
        assert result.conditions[0].condition == NodeConditionType.MEMORY_PRESSURE


# --- process_pods tests ---


class TestProcessPods:
    def test_empty_pods(self):
        result = process_pods([])
        assert result.total == 0
        assert result.running == 0
        assert result.pending == 0
        assert result.failed == 0

    def test_running_pods(self):
        pods = [
            {"metadata": {"name": "pod-1", "namespace": "default"}, "status": {"phase": "Running", "containerStatuses": []}},
            {"metadata": {"name": "pod-2", "namespace": "default"}, "status": {"phase": "Running", "containerStatuses": []}},
        ]
        result = process_pods(pods)
        assert result.total == 2
        assert result.running == 2
        assert result.pending == 0
        assert result.failed == 0

    def test_pending_and_failed_pods(self):
        pods = [
            {"metadata": {"name": "pod-1", "namespace": "default"}, "status": {"phase": "Pending", "containerStatuses": []}},
            {"metadata": {"name": "pod-2", "namespace": "default"}, "status": {"phase": "Failed", "containerStatuses": []}},
        ]
        result = process_pods(pods)
        assert result.total == 2
        assert result.running == 0
        assert result.pending == 1
        assert result.failed == 1

    def test_crash_looping_pod(self):
        pods = [
            {
                "metadata": {"name": "crash-pod", "namespace": "payments"},
                "status": {
                    "phase": "Running",
                    "conditions": [{"lastTransitionTime": "2024-01-01T10:00:00Z"}],
                    "containerStatuses": [
                        {
                            "restartCount": 15,
                            "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 5m"}},
                            "lastState": {},
                        }
                    ],
                },
            },
        ]
        result = process_pods(pods)
        assert len(result.crash_looping) == 1
        assert result.crash_looping[0].reason == PodFailureReason.CRASH_LOOP_BACK_OFF
        assert result.crash_looping[0].namespace == "payments"
        assert result.crash_looping[0].restart_count == 15

    def test_oom_killed_pod(self):
        pods = [
            {
                "metadata": {"name": "oom-pod", "namespace": "api"},
                "status": {
                    "phase": "Running",
                    "conditions": [],
                    "containerStatuses": [
                        {
                            "restartCount": 3,
                            "state": {"running": {"startedAt": "2024-01-01T10:05:00Z"}},
                            "lastState": {
                                "terminated": {
                                    "reason": "OOMKilled",
                                    "finishedAt": "2024-01-01T10:04:50Z",
                                    "message": "OOM",
                                }
                            },
                        }
                    ],
                },
            },
        ]
        result = process_pods(pods)
        assert len(result.oom_killed) == 1
        assert result.oom_killed[0].reason == PodFailureReason.OOM_KILLED
        assert result.oom_killed[0].namespace == "api"


# --- process_events tests ---


class TestProcessEvents:
    def test_empty_events(self):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        result = process_events([], cutoff)
        assert result == []

    def test_filters_old_events(self):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        events = [
            {
                "type": "Warning",
                "reason": "FailedScheduling",
                "involvedObject": {"kind": "Pod", "namespace": "default", "name": "old-pod"},
                "message": "old event",
                "count": 1,
                "firstTimestamp": old_ts,
                "lastTimestamp": old_ts,
            }
        ]
        result = process_events(events, cutoff)
        assert result == []

    def test_includes_recent_warning_events(self):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        events = [
            {
                "type": "Warning",
                "reason": "FailedScheduling",
                "involvedObject": {"kind": "Pod", "namespace": "default", "name": "my-pod"},
                "message": "no nodes available",
                "count": 3,
                "firstTimestamp": recent_ts,
                "lastTimestamp": recent_ts,
            }
        ]
        result = process_events(events, cutoff)
        assert len(result) == 1
        assert result[0].reason == "FailedScheduling"

    def test_excludes_normal_events(self):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        events = [
            {
                "type": "Normal",
                "reason": "Scheduled",
                "involvedObject": {"kind": "Pod", "namespace": "default", "name": "ok-pod"},
                "message": "successfully scheduled",
                "count": 1,
                "firstTimestamp": recent_ts,
                "lastTimestamp": recent_ts,
            }
        ]
        result = process_events(events, cutoff)
        assert result == []

    def test_max_100_events(self):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        events = [
            {
                "type": "Warning",
                "reason": f"Reason{i}",
                "involvedObject": {"kind": "Pod", "namespace": "default", "name": f"pod-{i}"},
                "message": f"event {i}",
                "count": 1,
                "firstTimestamp": recent_ts,
                "lastTimestamp": recent_ts,
            }
            for i in range(150)
        ]
        result = process_events(events, cutoff)
        assert len(result) == 100


# --- classify_health tests ---


class TestClassifyHealth:
    def test_healthy_cluster(self):
        nodes = NodesStatus(total=3, ready=3, notReady=[], conditions=[])
        pods = PodsStatus(total=10, running=10, pending=0, failed=0, crashLooping=[], oomKilled=[])
        assert classify_health(nodes, pods) == ClusterHealth.HEALTHY

    def test_critical_not_ready_node(self):
        nodes = NodesStatus(total=3, ready=2, notReady=["node-3"], conditions=[])
        pods = PodsStatus(total=10, running=10, pending=0, failed=0, crashLooping=[], oomKilled=[])
        assert classify_health(nodes, pods) == ClusterHealth.CRITICAL

    def test_critical_high_pod_failure_ratio(self):
        nodes = NodesStatus(total=3, ready=3, notReady=[], conditions=[])
        pods = PodsStatus(total=10, running=7, pending=2, failed=2, crashLooping=[], oomKilled=[])
        # (2 + 2) / 10 = 0.4 > 0.25
        assert classify_health(nodes, pods) == ClusterHealth.CRITICAL

    def test_degraded_pressure_condition(self):
        from skills.shared.models import NodeCondition, NodeConditionType

        cond = NodeCondition(
            nodeName="node-1",
            condition=NodeConditionType.MEMORY_PRESSURE,
            status=True,
            since=datetime.now(timezone.utc),
            message="low memory",
        )
        nodes = NodesStatus(total=3, ready=3, notReady=[], conditions=[cond])
        pods = PodsStatus(total=10, running=10, pending=0, failed=0, crashLooping=[], oomKilled=[])
        assert classify_health(nodes, pods) == ClusterHealth.DEGRADED

    def test_degraded_crash_looping(self):
        from skills.shared.models import PodFailure, PodFailureReason

        failure = PodFailure(
            name="crash-pod",
            namespace="default",
            reason=PodFailureReason.CRASH_LOOP_BACK_OFF,
            restartCount=5,
            lastTransition=datetime.now(timezone.utc),
            message="CrashLoop",
        )
        nodes = NodesStatus(total=3, ready=3, notReady=[], conditions=[])
        pods = PodsStatus(total=10, running=9, pending=0, failed=0, crashLooping=[failure], oomKilled=[])
        assert classify_health(nodes, pods) == ClusterHealth.DEGRADED

    def test_healthy_empty_cluster(self):
        nodes = NodesStatus(total=0, ready=0, notReady=[], conditions=[])
        pods = PodsStatus(total=0, running=0, pending=0, failed=0, crashLooping=[], oomKilled=[])
        assert classify_health(nodes, pods) == ClusterHealth.HEALTHY
