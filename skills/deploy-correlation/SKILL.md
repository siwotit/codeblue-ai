# Deploy Correlation Skill

**Classification**: script-heavy

The script performs the majority of work (CloudTrail queries, Kubernetes rollout queries, temporal proximity scoring, correlation score computation). This SKILL.md provides trigger conditions, input/output contracts, and interpretation guidance for the LLM.

---

## When to Invoke

Invoke this skill when **all** of the following conditions are met:

1. A `NormalizedAlert` has been produced by the alert-ingestion skill
2. The metric-baseline skill has completed (or been skipped due to missing metric context)
3. The orchestrator is in the `correlation` phase

**Always run this skill** — even when the metric baseline shows normal values. Recent changes may explain an alert even when metrics are not deviating (Requirement 11.5).

**Skip conditions**: None. This skill should always be invoked for every alert.

---

## Input Contract

The script accepts a `CorrelationRequest` JSON object:

```json
{
  "affectedResources": [
    {
      "type": "arn",
      "value": "arn:aws:ecs:us-east-1:123456789012:service/prod/payments",
      "displayName": "payments service"
    }
  ],
  "incidentTime": "2024-01-15T10:23:00Z",
  "lookbackWindow": "24h"
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `affectedResources` | ResourceIdentifier[] | yes | — | List of resources affected by the incident. Used to determine resource overlap with discovered changes. |
| `incidentTime` | ISO8601 | yes | — | The incident start time. Typically the alert's `firedAt` or the `anomalyStartTime` from metric-baseline if available. |
| `lookbackWindow` | string | no | `"24h"` | How far back to search for changes. Format: duration string (e.g., `"24h"`, `"48h"`). |

### ResourceIdentifier Schema

| Field | Type | Description |
|-------|------|-------------|
| `type` | enum | One of: `arn`, `k8s_resource`, `metric_dimension` |
| `value` | string | The resource identifier value (e.g., ARN, `namespace/kind/name`, dimension value) |
| `displayName` | string | Human-readable label for the resource |

### Constructing Input from Context

- `affectedResources` → from `alert.affectedResources`
- `incidentTime` → prefer `metricDeviation.anomalyStartTime` if available (more precise); fall back to `alert.firedAt`
- `lookbackWindow` → default `"24h"`; increase to `"48h"` if the alert references a gradually degrading metric

---

## Output Contract

The script returns a `CorrelatedChanges` JSON object wrapped in a `SkillResponse` envelope:

```json
{
  "status": "success",
  "message": "Found 3 correlated changes in 24h lookback window",
  "data": {
    "changes": [
      {
        "id": "ct-event-abc123",
        "type": "deployment",
        "source": "cloudtrail",
        "timestamp": "2024-01-15T10:15:00Z",
        "actor": "arn:aws:iam::123456789012:role/deploy-role",
        "description": "UpdateService on ecs:service/prod/payments — task definition changed from rev:42 to rev:43",
        "affectedResource": "arn:aws:ecs:us-east-1:123456789012:service/prod/payments",
        "details": {
          "eventName": "UpdateService",
          "taskDefinitionArn": "arn:aws:ecs:us-east-1:123456789012:task-definition/payments:43"
        },
        "temporalProximity": 0.92,
        "resourceOverlap": true,
        "correlationScore": 0.95
      }
    ],
    "lookbackWindow": "24h",
    "affectedResources": [
      {
        "type": "arn",
        "value": "arn:aws:ecs:us-east-1:123456789012:service/prod/payments",
        "displayName": "payments service"
      }
    ]
  }
}
```

### CorrelatedChanges Schema

| Field | Type | Description |
|-------|------|-------------|
| `changes` | Change[] | List of discovered changes, sorted by `correlationScore` descending |
| `lookbackWindow` | string | The lookback window that was queried |
| `affectedResources` | ResourceIdentifier[] | The input resources used for overlap detection |

### Change Schema

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique identifier for the change event |
| `type` | enum | One of: `deployment`, `config_change`, `iam_change`, `scaling_event`, `rollout`, `addon_update` |
| `source` | enum | One of: `cloudtrail`, `kubernetes` |
| `timestamp` | ISO8601 | When the change occurred |
| `actor` | string | Who or what made the change (IAM principal or K8s user) |
| `description` | string | Human-readable summary of what changed |
| `affectedResource` | string | The resource identifier affected by this change |
| `details` | object | Source-specific details (CloudTrail event fields, K8s rollout metadata) |
| `temporalProximity` | float | 0.0–1.0 score indicating how close in time this change is to the incident |
| `resourceOverlap` | boolean | Whether the change's affected resource matches any incident resource |
| `correlationScore` | float | 0.0–1.0 composite score combining temporal proximity and resource overlap |

### Empty Result Response

When no changes are found in the lookback window:

```json
{
  "status": "success",
  "message": "No changes found in 24h lookback window",
  "data": {
    "changes": [],
    "lookbackWindow": "24h",
    "affectedResources": [
      {
        "type": "arn",
        "value": "arn:aws:ecs:us-east-1:123456789012:service/prod/payments",
        "displayName": "payments service"
      }
    ]
  }
}
```

### Error Response

When a data source query fails but partial results are available:

```json
{
  "status": "success",
  "message": "Partial results: CloudTrail query succeeded, Kubernetes query failed (timeout after 15s)",
  "data": {
    "changes": [...],
    "lookbackWindow": "24h",
    "affectedResources": [...],
    "unavailableSources": ["kubernetes"],
    "reducedConfidence": true
  }
}
```

When all queries fail:

```json
{
  "status": "error",
  "message": "All change correlation sources unavailable: CloudTrail (access denied), Kubernetes (connection timeout)",
  "data": null
}
```

---

## Temporal Proximity Scoring

The `temporalProximity` score (0.0–1.0) quantifies how close in time a change occurred relative to the incident start. Changes closer to the incident are more likely causal.

### Scoring Formula

```
temporalProximity = max(0.0, 1.0 - (timeDeltaSeconds / lookbackWindowSeconds))
```

Where:
- `timeDeltaSeconds` = absolute difference between change timestamp and incident time
- `lookbackWindowSeconds` = total lookback window in seconds (e.g., 86400 for 24h)

### Scoring Examples

| Time Before Incident | temporalProximity (24h window) | Interpretation |
|---|---|---|
| 5 minutes | 0.997 | Extremely suspicious — immediate temporal correlation |
| 30 minutes | 0.979 | Very high — strong temporal correlation |
| 2 hours | 0.917 | High — likely related |
| 6 hours | 0.750 | Moderate — possible but less certain |
| 12 hours | 0.500 | Low — weak temporal correlation |
| 24 hours | 0.000 | Minimal — at the edge of the lookback window |

### Correlation Score Composition

The final `correlationScore` combines temporal proximity and resource overlap:

- **Both temporal proximity > 0 AND resource overlap is true**: The score is boosted above either factor alone. A change that is both recent and touches the same resource gets the highest score.
- **Only temporal proximity > 0 (no resource overlap)**: The score is based on temporal proximity alone but capped at a lower maximum (the change is recent but on a different resource).
- **Only resource overlap (low temporal proximity)**: The score reflects resource relevance but is reduced due to weak temporal signal.

**Key invariant**: When both temporal proximity increases AND resource overlap is present, the correlation score MUST be higher than when only one factor is present (Requirement 4.6).

### Scoring Guidelines

| Temporal Proximity | Resource Overlap | Expected Score Range |
|---|---|---|
| > 0.9 | true | 0.85–1.0 |
| > 0.9 | false | 0.50–0.70 |
| 0.5–0.9 | true | 0.60–0.85 |
| 0.5–0.9 | false | 0.30–0.50 |
| < 0.5 | true | 0.30–0.60 |
| < 0.5 | false | 0.05–0.30 |

---

## Interpretation Guidance

### Using Correlation Results for Hypothesis Generation

| Scenario | Interpretation | Action |
|---|---|---|
| **High-scoring change found** (score > 0.8) | Strong candidate for root cause. The change is both recent and touches the affected resource. | Lead the hypothesis with this change. Suggest rollback verification. |
| **Multiple moderate changes** (scores 0.4–0.8) | Several changes may have contributed. Consider cascading failure or compounding effects. | Present as multiple contributing factors. Rank by score. |
| **Only low-scoring changes** (scores < 0.4) | Changes exist but correlation is weak. The incident may have a non-deployment root cause. | De-prioritize change correlation. Weight log findings and metric anomalies higher. |
| **No changes found** | No recent changes in the blast radius. The incident is likely caused by external factors, gradual resource exhaustion, or upstream dependency failure. | Explicitly note absence of changes. Direct investigation toward metrics, logs, and upstream services. |

### Change Type Significance

| Change Type | Typical Impact | Weight Modifier |
|---|---|---|
| `deployment` | New code, new container image, task definition update | High — most common cause of sudden failures |
| `config_change` | Parameter store, secrets, environment variable changes | High — can cause immediate failures without code changes |
| `iam_change` | Permission modifications, role trust policy changes | Medium — can cause access failures |
| `scaling_event` | HPA scaling, replica count changes | Low — usually responsive, not causal |
| `rollout` | Kubernetes deployment rollout | High — equivalent to deployment |
| `addon_update` | EKS addon version change | Medium — can affect cluster networking and storage |

### Handling Partial Data

When `unavailableSources` is present in the response:

- Note the reduced coverage in hypothesis generation
- If only CloudTrail is available: Kubernetes-native changes (rollouts, HPA) will be missed
- If only Kubernetes is available: AWS-level changes (IAM, config) will be missed
- Always indicate reduced confidence when data sources are incomplete

---

## MCP Dependencies

This skill uses:

- **aws-api-mcp** — `lookupCloudTrailEvents` for CloudTrail change discovery
  - Filters: resource ARNs, event names matching write/update/create/delete patterns
  - Connection timeout: 30 seconds (orchestrator-enforced)
- **kubernetes-mcp** — `getDeployments`, `getReplicaSets`, `getEvents` for K8s change discovery
  - Filters: namespace from affected resources, event types related to scaling/rolling updates
  - Connection timeout: 30 seconds (orchestrator-enforced)

Skill execution timeout: 15 seconds

---

## Error Handling

The script handles failures gracefully per Requirement 4.8:

- **CloudTrail query failure**: Continue with Kubernetes data only; mark CloudTrail as unavailable
- **Kubernetes query failure**: Continue with CloudTrail data only; mark Kubernetes as unavailable
- **Both sources fail**: Return error response; orchestrator proceeds to next phase with no change data
- **Timeout**: Partial results returned if at least one source responded before timeout
- **Access denied**: Return error with specific source and reason

The orchestrator should proceed to the hypothesis phase regardless of this skill's outcome. Missing correlation data reduces overall confidence but does not block the workflow.
