"""Unit tests for the alert_ingestion skill."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure the project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# Import the skill functions from the hyphenated directory
sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent.parent / "skills" / "alert-ingestion"),
)
from alert_ingestion import (
    ingest_cloudwatch_alarm,
    ingest_grafana_alert,
    ingest_k8s_event,
    process_input,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cloudwatch_payload(
    alarm_name: str = "HighCPU",
    region: str = "us-east-1",
    state_change_time: str = "2024-01-15T10:30:00Z",
    **overrides,
) -> dict:
    base = {
        "AlarmName": alarm_name,
        "AlarmDescription": "CPU above threshold",
        "AWSAccountId": "123456789012",
        "NewStateValue": "ALARM",
        "NewStateReason": "Threshold crossed",
        "StateChangeTime": state_change_time,
        "Region": region,
        "AlarmArn": f"arn:aws:cloudwatch:{region}:123456789012:alarm:{alarm_name}",
        "OldStateValue": "OK",
        "Trigger": {
            "MetricName": "CPUUtilization",
            "Namespace": "AWS/EC2",
            "StatisticType": "Average",
            "Period": 300,
            "EvaluationPeriods": 3,
            "Threshold": 90.0,
            "ComparisonOperator": "GreaterThanThreshold",
            "Dimensions": [{"name": "InstanceId", "value": "i-1234567890abcdef0"}],
        },
        "OKActions": [],
        "AlarmActions": ["arn:aws:sns:us-east-1:123456789012:my-topic"],
        "InsufficientDataActions": [],
    }
    base.update(overrides)
    return base


def _grafana_payload(
    alert_name: str = "HighLatency",
    severity: str = "warning",
    starts_at: str = "2024-01-15T10:30:00Z",
    **overrides,
) -> dict:
    base = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": alert_name,
                    "severity": severity,
                    "namespace": "payments",
                    "service": "checkout-api",
                    "cluster": "prod-us-east-1",
                    "region": "us-east-1",
                },
                "annotations": {
                    "summary": f"{alert_name}: High latency detected",
                    "description": "P95 latency exceeds 500ms",
                    "runbook_url": "https://wiki.example.com/runbook",
                },
                "startsAt": starts_at,
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://grafana.example.com/d/abc123",
                "fingerprint": "fp-abc123",
                "values": {"latency_p95": 750.5},
            }
        ],
        "groupLabels": {},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://grafana.example.com",
    }
    for k, v in overrides.items():
        if k == "alerts":
            base["alerts"] = v
        elif k.startswith("labels."):
            base["alerts"][0]["labels"][k[7:]] = v
        elif k.startswith("annotations."):
            base["alerts"][0]["annotations"][k[12:]] = v
        else:
            base[k] = v
    return base


def _k8s_event_payload(
    kind: str = "Pod",
    name: str = "checkout-api-abc123",
    namespace: str = "payments",
    reason: str = "CrashLoopBackOff",
    **overrides,
) -> dict:
    base = {
        "apiVersion": "v1",
        "kind": "Event",
        "metadata": {
            "name": "checkout-api-abc123.event1",
            "namespace": namespace,
            "uid": "uid-event-001",
            "creationTimestamp": "2024-01-15T10:30:00Z",
        },
        "involvedObject": {
            "kind": kind,
            "name": name,
            "namespace": namespace,
            "uid": "uid-pod-001",
        },
        "reason": reason,
        "message": f"Back-off restarting failed container in pod {name}",
        "type": "Warning",
        "count": 5,
        "firstTimestamp": "2024-01-15T10:25:00Z",
        "lastTimestamp": "2024-01-15T10:30:00Z",
        "source": {"component": "kubelet", "host": "ip-10-0-1-42"},
        "clusterName": "prod-us-east-1",
        "region": "us-east-1",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# CloudWatch tests
# ---------------------------------------------------------------------------


class TestCloudWatchIngestion:
    def test_basic_alarm(self):
        payload = _cloudwatch_payload()
        result = ingest_cloudwatch_alarm(payload)

        assert result["status"] == "success"
        data = result["data"]
        assert data["source"] == "cloudwatch"
        assert data["title"] == "HighCPU"
        assert data["region"] == "us-east-1"
        assert data["state"] == "firing"
        assert data["firedAt"] == "2024-01-15T10:30:00Z"
        assert data["rawPayload"] == payload
        assert data["account"] == "123456789012"
        assert data["metricName"] == "CPUUtilization"
        assert data["metricNamespace"] == "AWS/EC2"
        assert data["threshold"] == 90.0

    def test_affected_resources_includes_arn(self):
        payload = _cloudwatch_payload()
        result = ingest_cloudwatch_alarm(payload)
        data = result["data"]

        arns = [r for r in data["affectedResources"] if r["type"] == "arn"]
        assert len(arns) == 1
        assert "alarm:HighCPU" in arns[0]["value"]

    def test_affected_resources_includes_dimensions(self):
        payload = _cloudwatch_payload()
        result = ingest_cloudwatch_alarm(payload)
        data = result["data"]

        dims = [r for r in data["affectedResources"] if r["type"] == "metric_dimension"]
        assert len(dims) == 1
        assert dims[0]["value"] == "InstanceId=i-1234567890abcdef0"

    def test_severity_critical_pagerduty_action(self):
        payload = _cloudwatch_payload(
            AlarmActions=["arn:aws:sns:us-east-1:123:PagerDuty-Alerts"]
        )
        result = ingest_cloudwatch_alarm(payload)
        assert result["data"]["severity"] == "critical"

    def test_severity_critical_cpu_threshold(self):
        payload = _cloudwatch_payload()
        payload["Trigger"]["Threshold"] = 96.0
        result = ingest_cloudwatch_alarm(payload)
        assert result["data"]["severity"] == "critical"

    def test_severity_medium_default(self):
        payload = _cloudwatch_payload()
        payload["Trigger"]["MetricName"] = "DiskReadOps"
        payload["AlarmActions"] = []
        result = ingest_cloudwatch_alarm(payload)
        assert result["data"]["severity"] == "medium"

    def test_severity_low_info_no_actions(self):
        payload = _cloudwatch_payload()
        payload["AlarmDescription"] = "Informational alert info"
        payload["AlarmActions"] = []
        payload["Trigger"]["MetricName"] = "SomethingElse"
        result = ingest_cloudwatch_alarm(payload)
        assert result["data"]["severity"] == "low"

    def test_resolved_state(self):
        payload = _cloudwatch_payload(NewStateValue="OK")
        result = ingest_cloudwatch_alarm(payload)
        assert result["data"]["state"] == "resolved"

    def test_missing_alarm_name(self):
        payload = _cloudwatch_payload()
        payload["AlarmName"] = ""
        result = ingest_cloudwatch_alarm(payload)
        assert result["status"] == "error"
        assert "AlarmName" in result["message"]

    def test_missing_region(self):
        payload = _cloudwatch_payload()
        del payload["Region"]
        result = ingest_cloudwatch_alarm(payload)
        assert result["status"] == "error"
        assert "Region" in result["message"]

    def test_idempotent(self):
        payload = _cloudwatch_payload()
        r1 = ingest_cloudwatch_alarm(payload)
        r2 = ingest_cloudwatch_alarm(payload)
        assert r1 == r2


# ---------------------------------------------------------------------------
# Grafana tests
# ---------------------------------------------------------------------------


class TestGrafanaIngestion:
    def test_basic_alert(self):
        payload = _grafana_payload()
        result = ingest_grafana_alert(payload)

        assert result["status"] == "success"
        data = result["data"]
        assert data["source"] == "grafana"
        assert data["title"] == "HighLatency: High latency detected"
        assert data["region"] == "us-east-1"
        assert data["state"] == "firing"
        assert data["cluster"] == "prod-us-east-1"
        assert data["namespace"] == "payments"
        assert data["workload"] == "checkout-api"
        assert data["rawPayload"] == payload

    def test_affected_resources(self):
        payload = _grafana_payload()
        result = ingest_grafana_alert(payload)
        data = result["data"]

        k8s_res = [r for r in data["affectedResources"] if r["type"] == "k8s_resource"]
        assert len(k8s_res) >= 1
        assert "prod-us-east-1/payments/checkout-api" in k8s_res[0]["value"]

    def test_severity_critical(self):
        payload = _grafana_payload(severity="critical")
        result = ingest_grafana_alert(payload)
        assert result["data"]["severity"] == "critical"

    def test_severity_warning(self):
        payload = _grafana_payload(severity="warning")
        result = ingest_grafana_alert(payload)
        assert result["data"]["severity"] == "medium"

    def test_severity_warning_tier1(self):
        payload = _grafana_payload()
        payload["alerts"][0]["labels"]["severity"] = "warning"
        payload["alerts"][0]["labels"]["tier"] = "1"
        result = ingest_grafana_alert(payload)
        assert result["data"]["severity"] == "high"

    def test_severity_info(self):
        payload = _grafana_payload(severity="info")
        result = ingest_grafana_alert(payload)
        assert result["data"]["severity"] == "low"

    def test_resolved_state(self):
        payload = _grafana_payload()
        payload["alerts"][0]["status"] = "resolved"
        payload["alerts"][0]["endsAt"] = "2024-01-15T11:00:00Z"
        result = ingest_grafana_alert(payload)
        data = result["data"]
        assert data["state"] == "resolved"
        assert data["resolvedAt"] == "2024-01-15T11:00:00Z"

    def test_missing_alerts(self):
        payload = _grafana_payload()
        payload["alerts"] = []
        result = ingest_grafana_alert(payload)
        assert result["status"] == "error"
        assert "alerts" in result["message"]

    def test_missing_alertname(self):
        payload = _grafana_payload()
        del payload["alerts"][0]["labels"]["alertname"]
        result = ingest_grafana_alert(payload)
        assert result["status"] == "error"
        assert "alertname" in result["message"]

    def test_metric_values_extracted(self):
        payload = _grafana_payload()
        result = ingest_grafana_alert(payload)
        data = result["data"]
        assert data["metricName"] == "latency_p95"
        assert data["currentValue"] == 750.5

    def test_idempotent(self):
        payload = _grafana_payload()
        r1 = ingest_grafana_alert(payload)
        r2 = ingest_grafana_alert(payload)
        assert r1 == r2


# ---------------------------------------------------------------------------
# Kubernetes tests
# ---------------------------------------------------------------------------


class TestK8sIngestion:
    def test_basic_event(self):
        payload = _k8s_event_payload()
        result = ingest_k8s_event(payload)

        assert result["status"] == "success"
        data = result["data"]
        assert data["source"] == "kubernetes"
        assert data["title"] == "Pod/checkout-api-abc123: CrashLoopBackOff"
        assert data["region"] == "us-east-1"
        assert data["state"] == "firing"
        assert data["cluster"] == "prod-us-east-1"
        assert data["namespace"] == "payments"
        assert data["firedAt"] == "2024-01-15T10:25:00Z"
        assert data["rawPayload"] == payload

    def test_affected_resources(self):
        payload = _k8s_event_payload()
        result = ingest_k8s_event(payload)
        data = result["data"]

        k8s_res = [r for r in data["affectedResources"] if r["type"] == "k8s_resource"]
        assert len(k8s_res) >= 1
        assert "payments/Pod/checkout-api-abc123" in k8s_res[0]["value"]

    def test_node_in_resources(self):
        payload = _k8s_event_payload()
        result = ingest_k8s_event(payload)
        data = result["data"]

        node_res = [
            r for r in data["affectedResources"]
            if r["type"] == "k8s_resource" and "Node" in r.get("displayName", "")
        ]
        assert len(node_res) == 1
        assert node_res[0]["value"] == "ip-10-0-1-42"

    def test_severity_critical_oomkilled(self):
        payload = _k8s_event_payload(reason="OOMKilled")
        result = ingest_k8s_event(payload)
        assert result["data"]["severity"] == "critical"

    def test_severity_high_crashloop(self):
        payload = _k8s_event_payload(reason="CrashLoopBackOff")
        result = ingest_k8s_event(payload)
        assert result["data"]["severity"] == "high"

    def test_severity_medium_unhealthy(self):
        payload = _k8s_event_payload(reason="Unhealthy")
        result = ingest_k8s_event(payload)
        assert result["data"]["severity"] == "medium"

    def test_severity_medium_unrecognized_reason(self):
        payload = _k8s_event_payload(reason="SomeNewReason")
        result = ingest_k8s_event(payload)
        assert result["data"]["severity"] == "medium"

    def test_workload_set_for_deployment(self):
        payload = _k8s_event_payload(kind="Deployment", name="checkout-api")
        result = ingest_k8s_event(payload)
        assert result["data"]["workload"] == "checkout-api"

    def test_workload_none_for_pod(self):
        payload = _k8s_event_payload(kind="Pod", name="checkout-api-abc123")
        result = ingest_k8s_event(payload)
        assert result["data"]["workload"] is None

    def test_missing_involved_object(self):
        payload = _k8s_event_payload()
        payload["involvedObject"] = {}
        result = ingest_k8s_event(payload)
        assert result["status"] == "error"

    def test_fallback_to_creation_timestamp(self):
        payload = _k8s_event_payload()
        del payload["firstTimestamp"]
        result = ingest_k8s_event(payload)
        assert result["status"] == "success"
        assert result["data"]["firedAt"] == "2024-01-15T10:30:00Z"

    def test_idempotent(self):
        payload = _k8s_event_payload()
        r1 = ingest_k8s_event(payload)
        r2 = ingest_k8s_event(payload)
        assert r1 == r2


# ---------------------------------------------------------------------------
# process_input routing tests
# ---------------------------------------------------------------------------


class TestProcessInput:
    def test_routes_cloudwatch(self):
        result = process_input({"source": "cloudwatch", "payload": _cloudwatch_payload()})
        assert result["status"] == "success"
        assert result["data"]["source"] == "cloudwatch"

    def test_routes_grafana(self):
        result = process_input({"source": "grafana", "payload": _grafana_payload()})
        assert result["status"] == "success"
        assert result["data"]["source"] == "grafana"

    def test_routes_kubernetes(self):
        result = process_input({"source": "kubernetes", "payload": _k8s_event_payload()})
        assert result["status"] == "success"
        assert result["data"]["source"] == "kubernetes"

    def test_missing_source(self):
        result = process_input({"payload": {}})
        assert result["status"] == "error"
        assert "source" in result["message"]

    def test_unrecognized_source(self):
        result = process_input({"source": "datadog", "payload": {}})
        assert result["status"] == "error"
        assert "Unrecognized source type" in result["message"]
        assert "datadog" in result["message"]

    def test_missing_payload(self):
        result = process_input({"source": "cloudwatch"})
        assert result["status"] == "error"
        assert "payload" in result["message"]

    def test_invalid_payload_type(self):
        result = process_input({"source": "cloudwatch", "payload": "not-a-dict"})
        assert result["status"] == "error"
        assert "JSON object" in result["message"]
