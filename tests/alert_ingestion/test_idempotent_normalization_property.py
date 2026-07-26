"""Property-based test for idempotent normalization.

**Property 2: Idempotent normalization**
**Validates: Requirement 1.7**

For any valid alert, verify normalize(normalize(a)) == normalize(a).
Calling the ingestion function twice with the same input must produce
identical results, proving deterministic ID generation and consistent
field mapping.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

# Ensure the project root is on the path
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


# --- Strategies ---


def cloudwatch_alarm_strategy():
    """Generate arbitrary valid CloudWatch alarm payloads."""
    return st.fixed_dictionaries({
        "AlarmName": st.text(min_size=1, max_size=50, alphabet=st.characters(
            whitelist_categories=("L", "N", "P"),
            blacklist_characters="\x00",
        )),
        "AlarmDescription": st.text(max_size=100),
        "AWSAccountId": st.from_regex(r"[0-9]{12}", fullmatch=True),
        "NewStateValue": st.sampled_from(["ALARM", "OK", "INSUFFICIENT_DATA"]),
        "NewStateReason": st.text(max_size=50),
        "StateChangeTime": st.from_regex(
            r"2024-0[1-9]-[012][0-9]T[01][0-9]:[0-5][0-9]:[0-5][0-9]Z",
            fullmatch=True,
        ),
        "Region": st.sampled_from([
            "us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1",
        ]),
        "AlarmArn": st.from_regex(
            r"arn:aws:cloudwatch:us-east-1:[0-9]{12}:alarm:[A-Za-z0-9_-]+",
            fullmatch=True,
        ),
        "OldStateValue": st.sampled_from(["OK", "ALARM", "INSUFFICIENT_DATA"]),
        "Trigger": st.fixed_dictionaries({
            "MetricName": st.sampled_from([
                "CPUUtilization", "FreeableMemory", "Latency",
                "5XXError", "DiskReadOps", "NetworkIn",
            ]),
            "Namespace": st.sampled_from([
                "AWS/EC2", "AWS/RDS", "AWS/ELB", "AWS/Lambda",
            ]),
            "StatisticType": st.sampled_from(["Average", "Sum", "Maximum"]),
            "Period": st.sampled_from([60, 300, 900]),
            "EvaluationPeriods": st.integers(min_value=1, max_value=10),
            "Threshold": st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False),
            "ComparisonOperator": st.sampled_from([
                "GreaterThanThreshold", "LessThanThreshold",
                "GreaterThanOrEqualToThreshold",
            ]),
            "Dimensions": st.lists(
                st.fixed_dictionaries({
                    "name": st.sampled_from(["InstanceId", "FunctionName", "DBInstanceIdentifier"]),
                    "value": st.text(min_size=1, max_size=30, alphabet=st.characters(
                        whitelist_categories=("L", "N", "Pd"),
                    )),
                }),
                min_size=0,
                max_size=3,
            ),
        }),
        "AlarmActions": st.lists(
            st.from_regex(r"arn:aws:sns:us-east-1:[0-9]{12}:[A-Za-z0-9_-]+", fullmatch=True),
            min_size=0,
            max_size=2,
        ),
        "OKActions": st.just([]),
        "InsufficientDataActions": st.lists(
            st.from_regex(r"arn:aws:sns:us-east-1:[0-9]{12}:[A-Za-z0-9_-]+", fullmatch=True),
            min_size=0,
            max_size=1,
        ),
    })


def grafana_alert_strategy():
    """Generate arbitrary valid Grafana alert payloads."""
    return st.fixed_dictionaries({
        "status": st.sampled_from(["firing", "resolved"]),
        "alerts": st.lists(
            st.fixed_dictionaries({
                "status": st.sampled_from(["firing", "resolved"]),
                "labels": st.fixed_dictionaries({
                    "alertname": st.text(min_size=1, max_size=40, alphabet=st.characters(
                        whitelist_categories=("L", "N"),
                    )),
                    "severity": st.sampled_from(["critical", "warning", "info", ""]),
                    "namespace": st.sampled_from(["payments", "api", "default", "monitoring"]),
                    "service": st.text(min_size=1, max_size=20, alphabet=st.characters(
                        whitelist_categories=("L", "N", "Pd"),
                    )),
                    "cluster": st.sampled_from(["prod-us-east-1", "staging-eu-west-1", ""]),
                    "region": st.sampled_from(["us-east-1", "eu-west-1", "ap-southeast-1"]),
                    "priority": st.sampled_from(["P1", "P2", "P3", ""]),
                    "tier": st.sampled_from(["1", "2", ""]),
                }),
                "annotations": st.fixed_dictionaries({
                    "summary": st.text(min_size=1, max_size=60),
                    "description": st.text(max_size=100),
                }),
                "startsAt": st.from_regex(
                    r"2024-0[1-9]-[012][0-9]T[01][0-9]:[0-5][0-9]:[0-5][0-9]Z",
                    fullmatch=True,
                ),
                "endsAt": st.sampled_from([
                    "0001-01-01T00:00:00Z",
                    "2024-01-15T12:00:00Z",
                ]),
                "fingerprint": st.from_regex(r"fp-[a-z0-9]{6}", fullmatch=True),
                "values": st.dictionaries(
                    keys=st.text(min_size=1, max_size=20, alphabet=st.characters(
                        whitelist_categories=("L", "N", "Pd"),
                    )),
                    values=st.floats(min_value=0.0, max_value=10000.0, allow_nan=False, allow_infinity=False),
                    min_size=0,
                    max_size=3,
                ),
            }),
            min_size=1,
            max_size=1,
        ),
        "groupLabels": st.just({}),
        "commonLabels": st.just({}),
        "commonAnnotations": st.just({}),
        "externalURL": st.just("http://grafana.example.com"),
    })


def k8s_event_strategy():
    """Generate arbitrary valid Kubernetes event payloads."""
    namespace = st.sampled_from(["payments", "api", "kube-system", "default", "monitoring"])
    return st.fixed_dictionaries({
        "apiVersion": st.just("v1"),
        "kind": st.just("Event"),
        "metadata": st.fixed_dictionaries({
            "name": st.text(min_size=1, max_size=30, alphabet=st.characters(
                whitelist_categories=("L", "N", "Pd"),
            )),
            "namespace": namespace,
            "uid": st.from_regex(r"uid-[a-z0-9]{8}", fullmatch=True),
            "creationTimestamp": st.from_regex(
                r"2024-0[1-9]-[012][0-9]T[01][0-9]:[0-5][0-9]:[0-5][0-9]Z",
                fullmatch=True,
            ),
        }),
        "involvedObject": st.fixed_dictionaries({
            "kind": st.sampled_from(["Pod", "Deployment", "StatefulSet", "Node", "Service"]),
            "name": st.text(min_size=1, max_size=30, alphabet=st.characters(
                whitelist_categories=("L", "N", "Pd"),
            )),
            "namespace": namespace,
            "uid": st.from_regex(r"uid-[a-z0-9]{8}", fullmatch=True),
        }),
        "reason": st.sampled_from([
            "CrashLoopBackOff", "OOMKilled", "ImagePullBackOff",
            "FailedScheduling", "NotReady", "BackOff", "Unhealthy",
            "FailedMount", "FailedAttachVolume", "Pulling", "Started",
        ]),
        "message": st.text(min_size=1, max_size=100),
        "type": st.sampled_from(["Warning", "Normal"]),
        "count": st.integers(min_value=1, max_value=100),
        "firstTimestamp": st.from_regex(
            r"2024-0[1-9]-[012][0-9]T[01][0-9]:[0-5][0-9]:[0-5][0-9]Z",
            fullmatch=True,
        ),
        "lastTimestamp": st.from_regex(
            r"2024-0[1-9]-[012][0-9]T[01][0-9]:[0-5][0-9]:[0-5][0-9]Z",
            fullmatch=True,
        ),
        "source": st.fixed_dictionaries({
            "component": st.sampled_from(["kubelet", "scheduler", "controller-manager"]),
            "host": st.from_regex(r"ip-10-0-[0-9]-[0-9]{1,3}", fullmatch=True),
        }),
        "clusterName": st.sampled_from(["prod-us-east-1", "staging-eu-west-1", ""]),
        "region": st.sampled_from(["us-east-1", "eu-west-1", "ap-southeast-1"]),
    })


# --- Property Tests ---


@given(payload=cloudwatch_alarm_strategy())
@settings(max_examples=200, deadline=None)
def test_cloudwatch_idempotent_normalization(payload: dict):
    """Property 2: Idempotent normalization for CloudWatch alerts.

    **Validates: Requirement 1.7**

    For any valid CloudWatch alarm payload, calling the ingestion function
    twice with the same input produces identical results.
    """
    result1 = ingest_cloudwatch_alarm(payload)
    result2 = ingest_cloudwatch_alarm(payload)

    assert result1 == result2, (
        f"Non-idempotent CloudWatch normalization.\n"
        f"First call: {result1}\n"
        f"Second call: {result2}"
    )


@given(payload=grafana_alert_strategy())
@settings(max_examples=200, deadline=None)
def test_grafana_idempotent_normalization(payload: dict):
    """Property 2: Idempotent normalization for Grafana alerts.

    **Validates: Requirement 1.7**

    For any valid Grafana alert payload, calling the ingestion function
    twice with the same input produces identical results.
    """
    result1 = ingest_grafana_alert(payload)
    result2 = ingest_grafana_alert(payload)

    assert result1 == result2, (
        f"Non-idempotent Grafana normalization.\n"
        f"First call: {result1}\n"
        f"Second call: {result2}"
    )


@given(payload=k8s_event_strategy())
@settings(max_examples=200, deadline=None)
def test_k8s_idempotent_normalization(payload: dict):
    """Property 2: Idempotent normalization for Kubernetes events.

    **Validates: Requirement 1.7**

    For any valid Kubernetes event payload, calling the ingestion function
    twice with the same input produces identical results.
    """
    result1 = ingest_k8s_event(payload)
    result2 = ingest_k8s_event(payload)

    assert result1 == result2, (
        f"Non-idempotent Kubernetes normalization.\n"
        f"First call: {result1}\n"
        f"Second call: {result2}"
    )
