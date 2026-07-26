"""Alert Ingestion Skill — normalizes alerts from CloudWatch, Grafana, and Kubernetes.

Accepts JSON on stdin with `source` and `payload` fields.
Returns SkillResponse JSON to stdout.

Usage:
    echo '{"source": "cloudwatch", "payload": {...}}' | uv run alert_ingestion.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Severity mapping
# ---------------------------------------------------------------------------

# CloudWatch critical metric patterns
_CW_CRITICAL_METRICS: dict[str, float] = {
    "CPUUtilization": 95.0,
    "FreeableMemory": 5.0,  # less-than threshold
    "UnHealthyHostCount": 0.0,  # greater-than threshold
}

_CW_PRODUCTION_METRICS = {
    "Latency",
    "Duration",
    "5XXError",
    "4XXError",
    "ErrorRate",
    "TargetResponseTime",
    "HTTPCode_ELB_5XX_Count",
    "HTTPCode_Target_5XX_Count",
}

_K8S_CRITICAL_REASONS = {"OOMKilled", "FailedScheduling", "NotReady"}
_K8S_HIGH_REASONS = {"CrashLoopBackOff", "ImagePullBackOff", "FailedMount"}
_K8S_MEDIUM_REASONS = {"BackOff", "Unhealthy", "FailedAttachVolume"}

_K8S_CRITICAL_NAMESPACES = {"kube-system", "monitoring", "istio-system"}


def _map_cloudwatch_severity(payload: dict[str, Any]) -> str:
    """Map CloudWatch alarm to severity using documented rules."""
    alarm_actions = payload.get("AlarmActions", [])
    alarm_description = (payload.get("AlarmDescription") or "").lower()
    trigger = payload.get("Trigger", {})
    metric_name = trigger.get("MetricName", "")
    threshold = trigger.get("Threshold")

    # Check for PagerDuty/OpsGenie SNS topics in actions
    has_paging_actions = any(
        "pagerduty" in action.lower() or "opsgenie" in action.lower()
        for action in alarm_actions
    )

    # Check critical metric thresholds
    is_critical_metric = False
    if metric_name in _CW_CRITICAL_METRICS and threshold is not None:
        crit_threshold = _CW_CRITICAL_METRICS[metric_name]
        if metric_name == "FreeableMemory":
            # FreeableMemory: critical when threshold is low (less-than comparison)
            is_critical_metric = threshold <= crit_threshold
        else:
            # Others: critical when threshold is high (greater-than comparison)
            is_critical_metric = threshold >= crit_threshold

    if has_paging_actions or is_critical_metric:
        return "critical"

    # Production metrics with alarm actions configured
    if metric_name in _CW_PRODUCTION_METRICS and alarm_actions:
        return "high"

    # Informational alarms with no alarm actions
    if not alarm_actions:
        if "info" in alarm_description or "warning" in alarm_description:
            return "low"

    # Non-critical metrics OR InsufficientDataActions triggered
    insufficient_actions = payload.get("InsufficientDataActions", [])
    if insufficient_actions:
        return "medium"

    # Default
    return "medium"


def _map_grafana_severity(alert: dict[str, Any]) -> str:
    """Map Grafana alert to severity using documented rules."""
    labels = alert.get("labels", {})
    severity_label = labels.get("severity", "").lower()
    priority_label = labels.get("priority", "").upper()
    tier_label = labels.get("tier", "")

    if severity_label == "critical" or priority_label == "P1":
        return "critical"

    if severity_label == "warning" and tier_label == "1":
        return "high"

    if severity_label == "warning":
        return "medium"

    if severity_label == "info":
        return "low"

    # No severity label → default medium
    if not severity_label:
        return "medium"

    return "medium"


def _map_k8s_severity(payload: dict[str, Any]) -> str:
    """Map Kubernetes event to severity using documented rules."""
    reason = payload.get("reason", "")
    involved_obj = payload.get("involvedObject", {})
    namespace = involved_obj.get("namespace", "")

    # Critical: OOMKilled, FailedScheduling on critical namespace, or NotReady
    if reason == "NotReady":
        return "critical"
    if reason in _K8S_CRITICAL_REASONS:
        if reason == "FailedScheduling" and namespace in _K8S_CRITICAL_NAMESPACES:
            return "critical"
        if reason == "OOMKilled":
            return "critical"

    # High reasons
    if reason in _K8S_HIGH_REASONS:
        return "high"

    # Medium reasons
    if reason in _K8S_MEDIUM_REASONS:
        return "medium"

    # All other Warning events → low, but default to medium for unrecognized
    return "medium"


# ---------------------------------------------------------------------------
# ID Generation (deterministic for idempotency)
# ---------------------------------------------------------------------------


def _generate_alert_id(source: str, source_alarm_id: str, fired_at: str) -> str:
    """Generate a deterministic ID from source + sourceAlarmId + firedAt."""
    raw = f"{source}:{source_alarm_id}:{fired_at}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Resource extraction helpers
# ---------------------------------------------------------------------------


def _extract_cloudwatch_resources(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract resource identifiers from a CloudWatch alarm payload."""
    resources: list[dict[str, Any]] = []
    alarm_arn = payload.get("AlarmArn", "")
    alarm_name = payload.get("AlarmName", "")

    if alarm_arn:
        resources.append({
            "type": "arn",
            "value": alarm_arn,
            "displayName": alarm_name or alarm_arn,
        })

    trigger = payload.get("Trigger", {})
    dimensions = trigger.get("Dimensions", [])
    for dim in dimensions:
        dim_name = dim.get("name", "")
        dim_value = dim.get("value", "")
        if dim_name and dim_value:
            resources.append({
                "type": "metric_dimension",
                "value": f"{dim_name}={dim_value}",
                "displayName": dim_value,
            })

    return resources


def _extract_grafana_resources(alert: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract resource identifiers from a Grafana alert."""
    resources: list[dict[str, Any]] = []
    labels = alert.get("labels", {})

    # Construct k8s_resource from labels
    cluster = labels.get("cluster", "")
    namespace = labels.get("namespace", "")
    service = labels.get("service", "")

    if cluster and namespace and service:
        resources.append({
            "type": "k8s_resource",
            "value": f"{cluster}/{namespace}/{service}",
            "displayName": labels.get("alertname", f"{namespace}/{service}"),
        })
    elif namespace and service:
        resources.append({
            "type": "k8s_resource",
            "value": f"{namespace}/{service}",
            "displayName": labels.get("alertname", f"{namespace}/{service}"),
        })
    elif namespace:
        resources.append({
            "type": "k8s_resource",
            "value": namespace,
            "displayName": labels.get("alertname", namespace),
        })

    # Instance label as metric_dimension
    instance = labels.get("instance", "")
    if instance:
        resources.append({
            "type": "metric_dimension",
            "value": instance,
            "displayName": instance,
        })

    return resources


def _extract_k8s_resources(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract resource identifiers from a Kubernetes event."""
    resources: list[dict[str, Any]] = []
    involved_obj = payload.get("involvedObject", {})

    kind = involved_obj.get("kind", "")
    name = involved_obj.get("name", "")
    namespace = involved_obj.get("namespace", "")

    if namespace and kind and name:
        resources.append({
            "type": "k8s_resource",
            "value": f"{namespace}/{kind}/{name}",
            "displayName": f"{kind}/{name}",
        })

    # Node from source.host
    source_info = payload.get("source", {})
    host = source_info.get("host", "")
    if host:
        resources.append({
            "type": "k8s_resource",
            "value": host,
            "displayName": f"Node/{host}",
        })

    return resources


# ---------------------------------------------------------------------------
# Ingestion functions
# ---------------------------------------------------------------------------


def ingest_cloudwatch_alarm(payload: dict[str, Any]) -> dict[str, Any]:
    """Map CloudWatch alarm fields to NormalizedAlert.

    Required fields: AlarmName, NewStateValue, StateChangeTime, Region, AlarmArn
    """
    missing = []
    if not payload.get("AlarmName"):
        missing.append("AlarmName")
    if not payload.get("StateChangeTime"):
        missing.append("StateChangeTime")
    if not payload.get("Region"):
        missing.append("Region")

    if missing:
        return _error(f"CloudWatch payload missing required fields: {', '.join(missing)}")

    alarm_name = payload["AlarmName"]
    state_change_time = payload["StateChangeTime"]
    region = payload["Region"]
    alarm_arn = payload.get("AlarmArn", "")
    account_id = payload.get("AWSAccountId")
    description = payload.get("AlarmDescription", "")
    new_state = payload.get("NewStateValue", "ALARM")
    trigger = payload.get("Trigger", {})

    source_alarm_id = alarm_arn or alarm_name
    alert_id = _generate_alert_id("cloudwatch", source_alarm_id, state_change_time)
    severity = _map_cloudwatch_severity(payload)

    state = "resolved" if new_state == "OK" else "firing"

    # Metric context
    metric_name = trigger.get("MetricName")
    metric_namespace = trigger.get("Namespace")
    threshold = trigger.get("Threshold")
    dimensions_list = trigger.get("Dimensions", [])
    dimensions = {d["name"]: d["value"] for d in dimensions_list if "name" in d and "value" in d} or None

    resources = _extract_cloudwatch_resources(payload)

    return _success({
        "id": alert_id,
        "source": "cloudwatch",
        "sourceAlarmId": source_alarm_id,
        "severity": severity,
        "title": alarm_name,
        "description": description or "",
        "state": state,
        "firedAt": state_change_time,
        "resolvedAt": None,
        "affectedResources": resources,
        "region": region,
        "account": account_id,
        "cluster": None,
        "namespace": None,
        "workload": None,
        "metricName": metric_name,
        "metricNamespace": metric_namespace,
        "dimensions": dimensions,
        "threshold": threshold,
        "currentValue": None,
        "rawPayload": payload,
    })


def ingest_grafana_alert(payload: dict[str, Any]) -> dict[str, Any]:
    """Map Grafana alert rule fields to NormalizedAlert.

    Required fields: alerts (non-empty list), alerts[0].labels.alertname, alerts[0].startsAt
    """
    alerts = payload.get("alerts", [])
    if not alerts:
        return _error("Grafana payload missing required field: alerts (empty or missing)")

    alert = alerts[0]
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})

    if not labels.get("alertname"):
        return _error("Grafana payload missing required field: alerts[0].labels.alertname")
    if not alert.get("startsAt"):
        return _error("Grafana payload missing required field: alerts[0].startsAt")

    alert_name = labels["alertname"]
    starts_at = alert["startsAt"]
    status = alert.get("status", "firing")
    fingerprint = alert.get("fingerprint", "")
    region = labels.get("region", "unknown")

    source_alarm_id = fingerprint or alert_name
    alert_id = _generate_alert_id("grafana", source_alarm_id, starts_at)
    severity = _map_grafana_severity(alert)

    state = "resolved" if status == "resolved" else "firing"
    ends_at = alert.get("endsAt")
    resolved_at = ends_at if state == "resolved" and ends_at else None

    # K8s context from labels
    cluster = labels.get("cluster") or None
    namespace = labels.get("namespace") or None
    workload = labels.get("service") or labels.get("deployment") or None

    resources = _extract_grafana_resources(alert)

    # Metric values from the values field
    values = alert.get("values", {})
    metric_name = None
    current_value = None
    if values:
        # Take the first metric
        metric_name = next(iter(values.keys()), None)
        current_value = next(iter(values.values()), None)

    title = annotations.get("summary", alert_name)
    description = annotations.get("description", "")

    return _success({
        "id": alert_id,
        "source": "grafana",
        "sourceAlarmId": source_alarm_id,
        "severity": severity,
        "title": title,
        "description": description,
        "state": state,
        "firedAt": starts_at,
        "resolvedAt": resolved_at,
        "affectedResources": resources,
        "region": region,
        "account": None,
        "cluster": cluster,
        "namespace": namespace,
        "workload": workload,
        "metricName": metric_name,
        "metricNamespace": None,
        "dimensions": None,
        "threshold": None,
        "currentValue": current_value,
        "rawPayload": payload,
    })


def ingest_k8s_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Map Kubernetes warning event fields to NormalizedAlert.

    Required fields: involvedObject (with kind, name, namespace),
                     reason, type, and a timestamp (firstTimestamp or metadata.creationTimestamp)
    """
    involved_obj = payload.get("involvedObject", {})
    metadata = payload.get("metadata", {})

    missing = []
    if not involved_obj.get("kind"):
        missing.append("involvedObject.kind")
    if not involved_obj.get("name"):
        missing.append("involvedObject.name")
    if not involved_obj.get("namespace"):
        missing.append("involvedObject.namespace")
    if not payload.get("reason"):
        missing.append("reason")

    # Timestamp: firstTimestamp preferred, then metadata.creationTimestamp
    fired_at = payload.get("firstTimestamp") or metadata.get("creationTimestamp")
    if not fired_at:
        missing.append("firstTimestamp or metadata.creationTimestamp")

    if missing:
        return _error(f"Kubernetes payload missing required fields: {', '.join(missing)}")

    kind = involved_obj["kind"]
    name = involved_obj["name"]
    namespace = involved_obj["namespace"]
    reason = payload["reason"]
    message = payload.get("message", "")
    cluster_name = payload.get("clusterName", "")
    region = payload.get("region", "unknown")
    event_uid = metadata.get("uid", "")

    source_alarm_id = event_uid or f"{namespace}/{kind}/{name}/{reason}"
    alert_id = _generate_alert_id("kubernetes", source_alarm_id, fired_at)
    severity = _map_k8s_severity(payload)

    # Workload: only if kind is a workload type
    workload_kinds = {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"}
    workload = name if kind in workload_kinds else None

    resources = _extract_k8s_resources(payload)

    title = f"{kind}/{name}: {reason}"
    description = message

    return _success({
        "id": alert_id,
        "source": "kubernetes",
        "sourceAlarmId": source_alarm_id,
        "severity": severity,
        "title": title,
        "description": description,
        "state": "firing",
        "firedAt": fired_at,
        "resolvedAt": None,
        "affectedResources": resources,
        "region": region,
        "account": None,
        "cluster": cluster_name or None,
        "namespace": namespace,
        "workload": workload,
        "metricName": None,
        "metricNamespace": None,
        "dimensions": None,
        "threshold": None,
        "currentValue": None,
        "rawPayload": payload,
    })


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _success(data: dict[str, Any]) -> dict[str, Any]:
    """Wrap data in a success SkillResponse."""
    return {"status": "success", "data": data}


def _error(message: str) -> dict[str, Any]:
    """Wrap message in an error SkillResponse."""
    return {"status": "error", "message": message, "data": None}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

_INGEST_DISPATCH = {
    "cloudwatch": ingest_cloudwatch_alarm,
    "grafana": ingest_grafana_alert,
    "kubernetes": ingest_k8s_event,
}


def process_input(input_data: dict[str, Any]) -> dict[str, Any]:
    """Process the input JSON and route to the appropriate ingestion function."""
    source = input_data.get("source", "")

    if not source:
        return _error("Input missing required field: 'source'")

    if source not in _INGEST_DISPATCH:
        return _error(
            f"Unrecognized source type: '{source}'. "
            f"Supported sources: cloudwatch, grafana, kubernetes"
        )

    payload = input_data.get("payload")
    if payload is None:
        return _error("Input missing required field: 'payload'")

    if not isinstance(payload, dict):
        return _error("'payload' must be a JSON object")

    return _INGEST_DISPATCH[source](payload)


def main() -> None:
    """Read JSON from stdin, process, and write result to stdout."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            result = _error("Empty input received on stdin")
        else:
            input_data = json.loads(raw)
            result = process_input(input_data)
    except json.JSONDecodeError as e:
        result = _error(f"Invalid JSON input: {e}")
    except Exception as e:
        result = _error(f"Unexpected error during alert ingestion: {e}")

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
