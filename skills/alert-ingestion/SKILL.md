# Alert Ingestion Skill

## Classification

**Script-heavy** — The Python script (`alert_ingestion.py`) performs all normalization logic including field extraction, severity mapping, and validation. This SKILL.md describes when to invoke the skill, the input/output contracts, and how to interpret results.

## When to Invoke (Trigger Conditions)

Invoke the alert-ingestion skill when ANY of the following occurs:

1. **CloudWatch Alarm state transition** — A CloudWatch Alarm transitions to `ALARM` state (detected via the `StateValue` field in the alarm payload changing to `"ALARM"`)
2. **Grafana alert fire** — A Grafana alert rule fires (detected via the `state` field transitioning to `"alerting"` or `"firing"` in the Grafana webhook payload)
3. **Kubernetes warning event** — A Kubernetes event of type `Warning` is emitted for a workload resource (Pod, Deployment, StatefulSet, DaemonSet, ReplicaSet, Job)

The skill should be invoked as the FIRST step in any incident workflow. All downstream skills depend on the `NormalizedAlert` output produced here.

## Input Contract

The script accepts a single JSON object on stdin with the following structure:

```json
{
  "source": "cloudwatch" | "grafana" | "kubernetes",
  "payload": { ... raw source payload ... }
}
```

### CloudWatch Alarm Payload

The `payload` field contains the raw CloudWatch Alarm state change notification:

```json
{
  "AlarmName": "string",
  "AlarmDescription": "string",
  "AWSAccountId": "string",
  "NewStateValue": "ALARM",
  "NewStateReason": "string",
  "StateChangeTime": "ISO8601 timestamp",
  "Region": "string",
  "AlarmArn": "string",
  "OldStateValue": "OK" | "INSUFFICIENT_DATA" | "ALARM",
  "Trigger": {
    "MetricName": "string",
    "Namespace": "string",
    "StatisticType": "string",
    "Period": 300,
    "EvaluationPeriods": 3,
    "Threshold": 90.0,
    "ComparisonOperator": "GreaterThanThreshold",
    "Dimensions": [
      { "name": "string", "value": "string" }
    ]
  },
  "OKActions": [],
  "AlarmActions": ["arn:aws:sns:..."],
  "InsufficientDataActions": []
}
```

### Grafana Alert Payload

The `payload` field contains the raw Grafana webhook notification:

```json
{
  "status": "firing" | "resolved",
  "alerts": [
    {
      "status": "firing",
      "labels": {
        "alertname": "string",
        "severity": "critical" | "warning" | "info",
        "namespace": "string",
        "service": "string",
        "cluster": "string",
        "region": "string"
      },
      "annotations": {
        "summary": "string",
        "description": "string",
        "runbook_url": "string"
      },
      "startsAt": "ISO8601 timestamp",
      "endsAt": "ISO8601 timestamp",
      "generatorURL": "string",
      "fingerprint": "string",
      "values": {
        "metric_name": 123.45
      }
    }
  ],
  "groupLabels": {},
  "commonLabels": {},
  "commonAnnotations": {},
  "externalURL": "string"
}
```

### Kubernetes Event Payload

The `payload` field contains the raw Kubernetes Event object:

```json
{
  "apiVersion": "v1",
  "kind": "Event",
  "metadata": {
    "name": "string",
    "namespace": "string",
    "uid": "string",
    "creationTimestamp": "ISO8601 timestamp"
  },
  "involvedObject": {
    "kind": "Pod" | "Deployment" | "StatefulSet" | "DaemonSet" | "ReplicaSet" | "Job",
    "name": "string",
    "namespace": "string",
    "uid": "string"
  },
  "reason": "string",
  "message": "string",
  "type": "Warning",
  "count": 1,
  "firstTimestamp": "ISO8601 timestamp",
  "lastTimestamp": "ISO8601 timestamp",
  "source": {
    "component": "string",
    "host": "string"
  },
  "clusterName": "string",
  "region": "string"
}
```

### Validation Rules

The script SHALL reject the payload with a structured error if:
- The `source` field is missing or is not one of: `cloudwatch`, `grafana`, `kubernetes`
- Required fields for populating the NormalizedAlert cannot be extracted (see field extraction below)

## Output Contract

On success, the script returns a JSON object to stdout:

```json
{
  "status": "success",
  "data": {
    "id": "string (UUID)",
    "source": "cloudwatch" | "grafana" | "kubernetes",
    "sourceAlarmId": "string",
    "severity": "critical" | "high" | "medium" | "low",
    "title": "string",
    "description": "string",
    "state": "firing" | "resolved",
    "firedAt": "ISO8601 timestamp",
    "resolvedAt": "ISO8601 timestamp | null",
    "affectedResources": [
      {
        "type": "arn" | "k8s_resource" | "metric_dimension",
        "value": "string",
        "displayName": "string"
      }
    ],
    "region": "string",
    "account": "string | null",
    "cluster": "string | null",
    "namespace": "string | null",
    "workload": "string | null",
    "metricName": "string | null",
    "metricNamespace": "string | null",
    "dimensions": "object | null",
    "threshold": "number | null",
    "currentValue": "number | null",
    "rawPayload": { "...original source payload..." }
  }
}
```

On error, the script returns:

```json
{
  "status": "error",
  "message": "Human-readable error description",
  "data": null
}
```

## Severity Mapping Rules

The script maps source-specific severity indicators to one of four normalized values:

### CloudWatch → Severity

| Condition | Severity |
|---|---|
| Alarm has actions targeting PagerDuty/OpsGenie SNS topics OR metric is in critical metric list (CPUUtilization > 95%, FreeableMemory < 5%, UnHealthyHostCount > 0) | `critical` |
| Alarm threshold breach on production metrics (latency, error rate, 5xx count) with `AlarmActions` configured | `high` |
| Alarm on non-critical metrics OR `InsufficientDataActions` triggered | `medium` |
| Informational alarms (description contains "info" or "warning") with no alarm actions | `low` |

**Default**: If severity cannot be determined from the above rules, assign `medium`.

### Grafana → Severity

| Condition | Severity |
|---|---|
| Label `severity` = `critical` OR `priority` = `P1` | `critical` |
| Label `severity` = `warning` AND label `tier` = `1` | `high` |
| Label `severity` = `warning` | `medium` |
| Label `severity` = `info` OR no severity label present | `low` |

**Default**: If no `severity` label exists, assign `medium`.

### Kubernetes → Severity

| Condition | Severity |
|---|---|
| Event reason is `OOMKilled`, `FailedScheduling` on critical namespace, or node `NotReady` | `critical` |
| Event reason is `CrashLoopBackOff`, `ImagePullBackOff`, or `FailedMount` | `high` |
| Event reason is `BackOff`, `Unhealthy`, or `FailedAttachVolume` | `medium` |
| All other Warning events | `low` |

**Default**: If the event reason is unrecognized, assign `medium`.

## Field Extraction Heuristics

### Resource Identifier Extraction

The script extracts at least one `ResourceIdentifier` per alert:

**CloudWatch**:
- Primary: Extract the `AlarmArn` as type `arn`
- Secondary: Extract each dimension from `Trigger.Dimensions` as type `metric_dimension` (e.g., `InstanceId=i-1234567890abcdef0`)
- Display name: Use `AlarmName` or the first dimension value

**Grafana**:
- Primary: Construct a resource identifier from labels (`namespace/service` or `cluster/namespace/service`) as type `k8s_resource`
- Secondary: If `instance` label exists, use it as type `metric_dimension`
- Display name: Use `alertname` label or annotation `summary`

**Kubernetes**:
- Primary: Construct from `involvedObject` as type `k8s_resource` with format `namespace/kind/name`
- Secondary: If the event source has a `host` field, include the node as type `k8s_resource`
- Display name: Use `involvedObject.kind/involvedObject.name`

### Timestamp Extraction

- **firedAt**: The moment the alert transitioned to a firing state
  - CloudWatch: `StateChangeTime`
  - Grafana: `alerts[0].startsAt`
  - Kubernetes: `firstTimestamp` (or `metadata.creationTimestamp` if firstTimestamp is absent)

### Kubernetes Context Fields

When the source is `kubernetes` or Grafana labels contain cluster/namespace info:
- `cluster`: From K8s event `clusterName` field or Grafana `cluster` label
- `namespace`: From K8s event `involvedObject.namespace` or Grafana `namespace` label
- `workload`: From K8s event `involvedObject.name` (if kind is Deployment/StatefulSet/DaemonSet) or Grafana `service`/`deployment` label

## Interpretation Guidance

After the script produces a `NormalizedAlert`:

1. **If `status: "error"`** — The payload was invalid or unrecognizable. Log the error and do NOT proceed with the incident workflow. Report the error to the engineer.

2. **If `status: "success"`** — Pass the `data` field (the NormalizedAlert) to the Orchestrator to begin the incident workflow.

3. **Severity interpretation for downstream skills**:
   - `critical`: Immediately trigger all signal collection skills in parallel. Set tight timeout budgets.
   - `high`: Trigger all signal collection skills. Normal timeout budgets.
   - `medium`: Trigger signal collection but deprioritize K8s health checks unless cluster field is populated.
   - `low`: Run metric baseline only. Skip deploy correlation unless explicitly requested.

4. **Cluster field presence** determines whether Kubernetes health skills are invoked:
   - Non-empty `cluster` → invoke k8s-cluster-health, pod-failure-triage, node-condition-check, eks-addon-status
   - Empty/null `cluster` → skip all Kubernetes health checks

5. **Idempotency**: Re-ingesting the same raw payload MUST produce an identical NormalizedAlert. The script uses deterministic ID generation (hash of source + sourceAlarmId + firedAt) to ensure this.

## Validates

- **Requirement 1.1**: CloudWatch alarm → NormalizedAlert with all required fields
- **Requirement 1.2**: Grafana alert → NormalizedAlert with all required fields
- **Requirement 1.3**: Kubernetes warning event → NormalizedAlert with all required fields
- **Requirement 1.4**: Affected resource identifier extraction
- **Requirement 1.5**: Severity mapping from source configuration
- **Requirement 1.6**: Source system tagging and ISO 8601 ingestion timestamp
- **Requirement 1.7**: Idempotent normalization
- **Requirement 1.8**: Reject payloads missing required fields with structured error
- **Requirement 1.9**: Reject unrecognized source types with structured error
- **Requirement 1.10**: Preserve raw payload in rawPayload field
- **Requirement 14.2**: SKILL.md contains trigger conditions, input/output contracts, reasoning heuristics
- **Requirement 14.10**: Script-heavy classification documented
