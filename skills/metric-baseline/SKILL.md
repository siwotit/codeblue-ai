# Metric Baseline Comparison Skill

**Classification**: script-heavy

The script performs the majority of work (metric data retrieval, statistical computation, deviation classification). This SKILL.md provides trigger conditions, input/output contracts, and interpretation guidance for the LLM.

---

## When to Invoke

Invoke this skill when **all** of the following conditions are met:

1. A `NormalizedAlert` has been produced by the alert-ingestion skill
2. The alert contains metric context — at least one of:
   - `metricName` and `metricNamespace` are populated
   - `dimensions` are available from the affected resource identifiers
   - The alert source is CloudWatch (which always implies metric context)
3. The orchestrator is in the `metric_analysis` phase

**Skip conditions**: If the alert contains no metric context and no metric can be inferred from affected resources, skip this skill and proceed to deploy-correlation.

---

## Input Contract

The script accepts a `BaselineRequest` JSON object:

```json
{
  "metricName": "string",
  "namespace": "string",
  "dimensions": {
    "key": "value"
  },
  "currentWindow": {
    "start": "ISO8601",
    "end": "ISO8601"
  },
  "baselineWindow": "7d" | "30d"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `metricName` | string | yes | CloudWatch metric name (e.g., `CPUUtilization`, `5XXError`) |
| `namespace` | string | yes | CloudWatch namespace (e.g., `AWS/ECS`, `AWS/ApplicationELB`) |
| `dimensions` | object | yes | Key-value pairs identifying the specific resource (e.g., `{"ClusterName": "prod-us-east-1", "ServiceName": "payments"}`) |
| `currentWindow` | TimeRange | yes | The observation window — typically the last 1 hour from alert `firedAt` |
| `baselineWindow` | string | yes | Either `"7d"` (default) or `"30d"` for the historical comparison period |

### Constructing Input from NormalizedAlert

- `metricName` → from `alert.metricName`
- `namespace` → from `alert.metricNamespace`
- `dimensions` → from `alert.dimensions` or extracted from `alert.affectedResources`
- `currentWindow.end` → `alert.firedAt`
- `currentWindow.start` → `alert.firedAt` minus 1 hour
- `baselineWindow` → default `"7d"`, use `"30d"` if configured or if 7d baseline data is sparse

---

## Output Contract

The script returns a `MetricDeviation` JSON object wrapped in a `SkillResponse` envelope:

```json
{
  "status": "success",
  "message": "Baseline comparison complete for CPUUtilization",
  "data": {
    "metricName": "CPUUtilization",
    "namespace": "AWS/ECS",
    "dimensions": {"ClusterName": "prod-us-east-1", "ServiceName": "payments"},
    "timeRange": {
      "start": "2024-01-15T10:00:00Z",
      "end": "2024-01-15T11:00:00Z"
    },
    "currentMean": 87.3,
    "currentP95": 94.1,
    "currentMax": 99.8,
    "baselineMean": 42.5,
    "baselineP95": 58.2,
    "baselineP99": 65.0,
    "baselineStdDev": 8.7,
    "deviationFactor": 5.15,
    "classification": "critical",
    "confidence": 0.92,
    "anomalyStartTime": "2024-01-15T10:23:00Z",
    "dataPoints": [...],
    "baselineDataPoints": [...]
  }
}
```

### MetricDeviation Schema

| Field | Type | Description |
|-------|------|-------------|
| `metricName` | string | The metric that was compared |
| `namespace` | string | CloudWatch namespace |
| `dimensions` | object | Resource dimensions used for the query |
| `timeRange` | TimeRange | The current observation window queried |
| `currentMean` | float | Mean of current window values |
| `currentP95` | float | 95th percentile of current window |
| `currentMax` | float | Maximum value in current window |
| `baselineMean` | float | Mean across the baseline period |
| `baselineP95` | float | 95th percentile of baseline |
| `baselineP99` | float | 99th percentile of baseline |
| `baselineStdDev` | float | Standard deviation of baseline |
| `deviationFactor` | float | Number of standard deviations from baseline mean: `(currentMean - baselineMean) / baselineStdDev` |
| `classification` | enum | One of: `normal`, `elevated`, `anomalous`, `critical` |
| `confidence` | float | 0.0–1.0 confidence in the assessment |
| `anomalyStartTime` | ISO8601 or null | Earliest datapoint exceeding elevated threshold (present when classification is `anomalous` or `critical`) |
| `dataPoints` | array | Current window metric data points |
| `baselineDataPoints` | array | Baseline period metric data points |

### Error Response

```json
{
  "status": "error",
  "message": "Metric data retrieval failed: timeout after 15s querying CloudWatch for CPUUtilization",
  "data": null
}
```

---

## Interpretation Guidance

### Deviation Classifications

Use the `classification` field to determine next actions and weight in hypothesis generation:

| Classification | Deviation Factor | Interpretation | Action Guidance |
|---|---|---|---|
| **normal** | < 2σ | Current values are within expected variance. The metric does not explain the alert. | De-prioritize this metric as a root cause signal. Investigate other sources (logs, changes). |
| **elevated** | ≥ 2σ, < 3σ | Values are above typical but may not indicate a problem. Could be legitimate load increase. | Note as supporting context. Do not treat as primary evidence alone. Combine with other signals. |
| **anomalous** | ≥ 3σ, < 5σ | Statistically significant deviation. Strong indicator of abnormal behavior. | Treat as primary evidence for hypothesis generation. Check `anomalyStartTime` for correlation with recent changes. |
| **critical** | ≥ 5σ | Extreme deviation far outside historical norms. Almost certainly indicates a genuine incident. | Highest-weight evidence. Use `anomalyStartTime` as the anchor point for deploy-correlation temporal scoring. |

### Confidence Interpretation

| Confidence Range | Meaning |
|---|---|
| 0.8–1.0 | High confidence — dense baseline data, stable historical pattern |
| 0.5–0.8 | Moderate confidence — adequate baseline data, some variance in history |
| 0.0–0.5 | Low confidence — sparse baseline (< 24h of data). The deviation classification may be unreliable. Treat results as directional, not conclusive. |

**Key rule**: Confidence is capped at 0.5 when the baseline period contains fewer than 24 hours of metric data. This prevents over-weighting assessments based on insufficient history.

### Monotonic Severity Guarantee

The classification is **monotonically non-decreasing** with respect to the deviation factor: as the deviation factor increases, the classification can only stay the same or increase in severity. This means:

- A metric that was `elevated` at time T cannot be reclassified as `normal` at time T+1 if the deviation factor has increased
- This property is enforced by the script and can be relied upon for reasoning

### Using anomalyStartTime

When `classification` is `anomalous` or `critical`, the `anomalyStartTime` field identifies when the deviation first crossed the elevated threshold (≥ 2σ). Use this to:

1. **Correlate with changes**: Feed `anomalyStartTime` to the deploy-correlation skill as the anchor for temporal proximity scoring
2. **Narrow log search**: Focus log-triage queries around the anomaly start time
3. **Estimate duration**: Compare `anomalyStartTime` to `timeRange.end` to understand how long the anomaly has persisted

---

## MCP Dependencies

This skill uses the **cloudwatch-mcp** for metric data retrieval:

- `getMetricData` — fetches metric values for both current and baseline windows
- Connection timeout: 30 seconds (orchestrator-enforced)
- Skill execution timeout: 15 seconds

---

## Error Handling

The script handles failures gracefully per Requirement 2.9:

- **Metric retrieval timeout**: Returns error response, does not block workflow
- **Empty metric data**: Returns assessment with confidence capped at 0.5
- **Invalid dimensions**: Returns error response indicating which dimensions could not be resolved
- **MCP connection failure**: Returns error response with unavailability reason

The orchestrator should proceed to the next workflow phase regardless of this skill's outcome.
