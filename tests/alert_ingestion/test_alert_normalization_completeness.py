"""Property-based test: Alert normalization completeness.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.6**

Generates arbitrary valid alert payloads for all three source types
(CloudWatch, Grafana, Kubernetes) and verifies that every successful
normalization produces all required fields in the NormalizedAlert output.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

# Ensure the project root and skill directory are importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent.parent / "skills" / "alert-ingestion"),
)

from alert_ingestion import (
    ingest_cloudwatch_alarm,
    ingest_grafana_alert,
    ingest_k8s_event,
)

# ---------------------------------------------------------------------------
# ISO 8601 pattern for validation
# ---------------------------------------------------------------------------

_ISO8601_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
)

VALID_SOURCES = {"cloudwatch", "grafana", "kubernetes"}
VALID_SEVERITIES = {"critical", "high", "medium", "low"}

# ---------------------------------------------------------------------------
# Strategies for generating valid payloads
# ---------------------------------------------------------------------------

_non_empty_text = st.text(
    alphabet=st.characters(categories=("L", "N", "P", "S"), exclude_characters="\x00"),
    min_size=1,
    max_size=50,
)

_iso_timestamp = st.from_regex(
    r"20[12]\d-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\dZ",
    fullmatch=True,
)

_region = st.sampled_from([
    "us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1",
    "us-east-2", "eu-central-1", "ap-northeast-1",
])

_account_id = st.from_regex(r"\d{12}", fullmatch=True)


# --- CloudWatch payload strategy ---

_cw_metric_name = st.sampled_from([
    "CPUUtilization", "NetworkIn", "DiskReadOps", "Latency",
    "5XXError", "FreeableMemory", "UnHealthyHostCount",
    "RequestCount", "Duration",
])

_cw_namespace = st.sampled_from([
    "AWS/EC2", "AWS/ELB", "AWS/RDS", "AWS/Lambda", "AWS/ECS",
])

_cw_dimension = st.fixed_dictionaries({
    "name": st.sampled_from(["InstanceId", "LoadBalancerName", "FunctionName", "ClusterName"]),
    "value": _non_empty_text,
})


@st.composite
def cloudwatch_payloads(draw):
    """Generate a valid CloudWatch alarm payload."""
    alarm_name = draw(_non_empty_text)
    region = draw(_region)
    state_change_time = draw(_iso_timestamp)
    account_id = draw(_account_id)
    metric_name = draw(_cw_metric_name)
    namespace = draw(_cw_namespace)
    threshold = draw(st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False))
    dimensions = draw(st.lists(_cw_dimension, min_size=1, max_size=3))

    alarm_arn = f"arn:aws:cloudwatch:{region}:{account_id}:alarm:{alarm_name}"

    alarm_actions = draw(st.lists(
        st.sampled_from([
            f"arn:aws:sns:{region}:{account_id}:my-topic",
            f"arn:aws:sns:{region}:{account_id}:PagerDuty-Alerts",
            f"arn:aws:sns:{region}:{account_id}:OpsGenie-Alerts",
        ]),
        min_size=0,
        max_size=2,
    ))

    return {
        "AlarmName": alarm_name,
        "AlarmDescription": draw(st.text(max_size=100)),
        "AWSAccountId": account_id,
        "NewStateValue": draw(st.sampled_from(["ALARM", "OK", "INSUFFICIENT_DATA"])),
        "NewStateReason": "Threshold crossed",
        "StateChangeTime": state_change_time,
        "Region": region,
        "AlarmArn": alarm_arn,
        "OldStateValue": "OK",
        "Trigger": {
            "MetricName": metric_name,
            "Namespace": namespace,
            "StatisticType": "Average",
            "Period": 300,
            "EvaluationPeriods": 3,
            "Threshold": threshold,
            "ComparisonOperator": "GreaterThanThreshold",
            "Dimensions": dimensions,
        },
        "AlarmActions": alarm_actions,
        "OKActions": [],
        "InsufficientDataActions": draw(st.lists(
            st.just(f"arn:aws:sns:{region}:{account_id}:insufficient-data"),
            min_size=0,
            max_size=1,
        )),
    }


# --- Grafana payload strategy ---

_grafana_severity = st.sampled_from(["critical", "warning", "info", ""])
_grafana_priority = st.sampled_from(["P1", "P2", "P3", ""])


@st.composite
def grafana_payloads(draw):
    """Generate a valid Grafana alert payload."""
    alert_name = draw(_non_empty_text)
    starts_at = draw(_iso_timestamp)
    status = draw(st.sampled_from(["firing", "resolved"]))
    region = draw(_region)
    namespace = draw(_non_empty_text)
    service = draw(_non_empty_text)
    cluster = draw(_non_empty_text)
    severity = draw(_grafana_severity)
    priority = draw(_grafana_priority)

    labels = {
        "alertname": alert_name,
        "namespace": namespace,
        "service": service,
        "cluster": cluster,
        "region": region,
    }
    if severity:
        labels["severity"] = severity
    if priority:
        labels["priority"] = priority

    tier = draw(st.sampled_from(["", "1", "2"]))
    if tier:
        labels["tier"] = tier

    alert = {
        "status": status,
        "labels": labels,
        "annotations": {
            "summary": f"{alert_name}: something happened",
            "description": draw(st.text(max_size=100)),
        },
        "startsAt": starts_at,
        "endsAt": "2024-12-31T23:59:59Z" if status == "resolved" else "0001-01-01T00:00:00Z",
        "fingerprint": draw(_non_empty_text),
        "values": draw(st.one_of(
            st.just({}),
            st.fixed_dictionaries({"metric_value": st.floats(
                min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False
            )}),
        )),
    }

    return {
        "status": status,
        "alerts": [alert],
        "groupLabels": {},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://grafana.example.com",
    }


# --- Kubernetes event payload strategy ---

_k8s_kind = st.sampled_from(["Pod", "Deployment", "StatefulSet", "ReplicaSet", "Node", "DaemonSet"])
_k8s_reason = st.sampled_from([
    "CrashLoopBackOff", "OOMKilled", "ImagePullBackOff", "FailedScheduling",
    "NotReady", "BackOff", "Unhealthy", "FailedMount", "FailedAttachVolume",
    "CreateContainerError", "SandboxError", "Pulling", "Started",
])


@st.composite
def k8s_event_payloads(draw):
    """Generate a valid Kubernetes event payload."""
    kind = draw(_k8s_kind)
    name = draw(_non_empty_text)
    namespace = draw(_non_empty_text)
    reason = draw(_k8s_reason)
    fired_at = draw(_iso_timestamp)
    region = draw(_region)
    cluster_name = draw(_non_empty_text)

    return {
        "apiVersion": "v1",
        "kind": "Event",
        "metadata": {
            "name": f"{name}.event1",
            "namespace": namespace,
            "uid": draw(_non_empty_text),
            "creationTimestamp": fired_at,
        },
        "involvedObject": {
            "kind": kind,
            "name": name,
            "namespace": namespace,
            "uid": draw(_non_empty_text),
        },
        "reason": reason,
        "message": draw(st.text(max_size=200)),
        "type": "Warning",
        "count": draw(st.integers(min_value=1, max_value=100)),
        "firstTimestamp": fired_at,
        "lastTimestamp": fired_at,
        "source": {
            "component": "kubelet",
            "host": draw(_non_empty_text),
        },
        "clusterName": cluster_name,
        "region": region,
    }


# ---------------------------------------------------------------------------
# Property: Alert normalization completeness
# ---------------------------------------------------------------------------


def _assert_normalized_alert_complete(result: dict, original_payload: dict) -> None:
    """Assert that a successful normalization result has all required fields."""
    assert result["status"] == "success", f"Expected success, got: {result}"
    data = result["data"]

    # 1. id is non-empty (Req 1.1)
    assert data["id"], "id must be non-empty"
    assert isinstance(data["id"], str)
    assert len(data["id"]) > 0

    # 2. source is one of the valid sources (Req 1.2)
    assert data["source"] in VALID_SOURCES, (
        f"source must be one of {VALID_SOURCES}, got: {data['source']}"
    )

    # 3. severity is one of the valid severities (Req 1.3)
    assert data["severity"] in VALID_SEVERITIES, (
        f"severity must be one of {VALID_SEVERITIES}, got: {data['severity']}"
    )

    # 4. title is non-empty
    assert data["title"], "title must be non-empty"
    assert isinstance(data["title"], str)
    assert len(data["title"]) > 0

    # 5. firedAt is a valid ISO 8601 timestamp
    assert data["firedAt"], "firedAt must be non-empty"
    assert _ISO8601_PATTERN.match(data["firedAt"]), (
        f"firedAt must be ISO 8601, got: {data['firedAt']}"
    )

    # 6. affectedResources has at least 1 entry (Req 1.4)
    assert isinstance(data["affectedResources"], list), "affectedResources must be a list"
    assert len(data["affectedResources"]) >= 1, (
        "affectedResources must have at least 1 entry"
    )
    # Each resource must have type and value
    for resource in data["affectedResources"]:
        assert "type" in resource, "each resource must have a 'type' field"
        assert "value" in resource, "each resource must have a 'value' field"
        assert resource["value"], "resource value must be non-empty"

    # 7. region is non-empty
    assert data["region"], "region must be non-empty"
    assert isinstance(data["region"], str)
    assert len(data["region"]) > 0

    # 8. rawPayload is the original payload (Req 1.6)
    assert data["rawPayload"] == original_payload, (
        "rawPayload must preserve the original payload"
    )


@given(payload=cloudwatch_payloads())
@settings(max_examples=100, deadline=None)
def test_cloudwatch_normalization_completeness(payload: dict) -> None:
    """Property 1: Any valid CloudWatch alarm produces a complete NormalizedAlert.

    **Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.6**
    """
    result = ingest_cloudwatch_alarm(payload)
    _assert_normalized_alert_complete(result, payload)


@given(payload=grafana_payloads())
@settings(max_examples=100, deadline=None)
def test_grafana_normalization_completeness(payload: dict) -> None:
    """Property 1: Any valid Grafana alert produces a complete NormalizedAlert.

    **Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.6**
    """
    result = ingest_grafana_alert(payload)
    _assert_normalized_alert_complete(result, payload)


@given(payload=k8s_event_payloads())
@settings(max_examples=100, deadline=None)
def test_k8s_normalization_completeness(payload: dict) -> None:
    """Property 1: Any valid Kubernetes event produces a complete NormalizedAlert.

    **Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.6**
    """
    result = ingest_k8s_event(payload)
    _assert_normalized_alert_complete(result, payload)
