# Log Triage Skill

**Classification**: script-heavy

The script performs the majority of work (CloudWatch Logs Insights queries, pod log queries, error count comparison against 7-day baseline, finding classification). This SKILL.md provides trigger conditions, input/output contracts, and interpretation guidance for the LLM.

---

## When to Invoke

Invoke this skill when **all** of the following conditions are met:

1. The metric-baseline skill has completed (or been skipped/timed out)
2. Log groups can be identified from the incident context — at least one of:
   - The `NormalizedAlert` contains `affectedResources` with ARNs that map to known log groups
   - The alert has a non-empty `namespace` field (Kubernetes workload logs)
   - The alert source is CloudWatch and the alarm is associated with a log group
   - Log groups are explicitly provided by the orchestrator based on resource mapping
3. The orchestrator is in the `log_triage` phase

**Skip conditions**: If no log groups can be identified from the alert context and no Kubernetes namespaces are present, skip this skill and proceed to the hypothesis-engine phase.

---

## Input Contract

The script accepts a `LogTriageRequest` JSON object:

```json
{
  "logGroups": ["/aws/ecs/prod-payments", "/aws/lambda/order-processor"],
  "namespaces": ["payments", "orders"],
  "timeRange": {
    "start": "2024-01-15T10:00:00Z",
    "end": "2024-01-15T11:00:00Z"
  },
  "errorPatterns": ["ERROR", "FATAL", "Exception", "Timeout", "ConnectionRefused"]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `logGroups` | string[] | yes | CloudWatch log group names to query. Must contain at least one entry. |
| `namespaces` | string[] | no | Kubernetes namespaces for pod log queries. When provided, the script also queries pod logs for crash and exception patterns. |
| `timeRange` | TimeRange | yes | The observation window — typically the last 1 hour from the alert's `firedAt` timestamp. |
| `errorPatterns` | string[] | no | Override the default error patterns to search for. If omitted, the script uses the default set: `["ERROR", "FATAL", "Exception", "Timeout", "ConnectionRefused"]`. |

### Constructing Input from NormalizedAlert

- `logGroups` → derived from `alert.affectedResources` ARNs mapped to their CloudWatch log groups (e.g., ECS service → `/aws/ecs/<cluster>/<service>`, Lambda → `/aws/lambda/<function-name>`)
- `namespaces` → from `alert.namespace` (if present), or extracted from affected Kubernetes resource identifiers
- `timeRange.end` → `alert.firedAt`
- `timeRange.start` → `alert.firedAt` minus 1 hour
- `errorPatterns` → omit to use defaults, or provide custom patterns based on the service's known error signatures

### Default Error Patterns

When `errorPatterns` is omitted, the script searches for:

| Pattern | Rationale |
|---------|-----------|
| `ERROR` | Standard application error log level |
| `FATAL` | Unrecoverable application failures |
| `Exception` | Unhandled exceptions (Java, Python, .NET stack traces) |
| `Timeout` | Network or processing timeouts indicating latency or connectivity issues |
| `ConnectionRefused` | Downstream service unreachable — potential dependency failure |

---

## Output Contract

The script returns a `LogFindings` JSON object wrapped in a `SkillResponse` envelope:

```json
{
  "status": "success",
  "message": "Log triage complete: 3 findings across 2 log groups (1 new, 2 pre-existing)",
  "data": {
    "findings": [
      {
        "pattern": "ConnectionRefusedError: payments-db:5432",
        "count": 247,
        "firstSeen": "2024-01-15T10:23:14Z",
        "lastSeen": "2024-01-15T10:59:47Z",
        "isNew": true,
        "severity": "error",
        "sampleLogLines": [
          "2024-01-15T10:23:14Z ERROR ConnectionRefusedError: payments-db:5432 - Connection refused",
          "2024-01-15T10:23:15Z ERROR Failed to connect to payments-db:5432 after 3 retries",
          "2024-01-15T10:24:01Z ERROR ConnectionRefusedError: payments-db:5432 - Connection refused",
          "2024-01-15T10:25:33Z ERROR Database connection pool exhausted, all connections failed",
          "2024-01-15T10:27:12Z ERROR ConnectionRefusedError: payments-db:5432 - ECONNREFUSED"
        ],
        "logGroup": "/aws/ecs/prod-payments",
        "logStream": "payments-service/main/abc123"
      },
      {
        "pattern": "TimeoutError: upstream response timeout",
        "count": 89,
        "firstSeen": "2024-01-15T10:24:00Z",
        "lastSeen": "2024-01-15T10:58:30Z",
        "isNew": false,
        "severity": "error",
        "sampleLogLines": [
          "2024-01-15T10:24:00Z ERROR TimeoutError: upstream response timeout after 30000ms",
          "2024-01-15T10:25:12Z ERROR TimeoutError: upstream response timeout after 30000ms",
          "2024-01-15T10:30:45Z ERROR TimeoutError: upstream response timeout after 30000ms"
        ],
        "logGroup": "/aws/ecs/prod-payments",
        "logStream": "payments-service/main/abc123"
      }
    ],
    "queriedLogGroups": ["/aws/ecs/prod-payments", "/aws/lambda/order-processor"],
    "timeRange": {
      "start": "2024-01-15T10:00:00Z",
      "end": "2024-01-15T11:00:00Z"
    },
    "totalErrorCount": 336,
    "baselineErrorCount": 12
  }
}
```

### LogFindings Schema

| Field | Type | Description |
|-------|------|-------------|
| `findings` | LogFinding[] | List of distinct error patterns found in the queried logs |
| `queriedLogGroups` | string[] | All log groups that were successfully queried (excludes inaccessible ones) |
| `timeRange` | TimeRange | The observation window that was queried |
| `totalErrorCount` | int | Total number of error-level log entries across all patterns in the current window |
| `baselineErrorCount` | int | Average error count per equivalent time window from the 7-day baseline period |

### LogFinding Schema

| Field | Type | Description |
|-------|------|-------------|
| `pattern` | string | The error pattern or exception class (e.g., `"ConnectionRefusedError: payments-db:5432"`) |
| `count` | int | Number of occurrences in the current time window |
| `firstSeen` | ISO8601 | Earliest occurrence in the current window |
| `lastSeen` | ISO8601 | Most recent occurrence in the current window |
| `isNew` | boolean | `true` if this pattern was NOT present in the 7-day baseline; `false` if pre-existing |
| `severity` | enum | One of: `error`, `warning`, `fatal` |
| `sampleLogLines` | string[] | Representative log lines for this pattern (maximum 5) |
| `logGroup` | string | CloudWatch log group where this pattern was found |
| `logStream` | string or null | Specific log stream (when identifiable) |

### Error Response

```json
{
  "status": "error",
  "message": "Log triage failed: all specified log groups are inaccessible (permission denied)",
  "data": null
}
```

### Partial Success Response

When some log groups are inaccessible but others succeed, the script returns a success response with findings from accessible groups. Inaccessible groups are omitted from `queriedLogGroups` and a note is included in the `message` field:

```json
{
  "status": "success",
  "message": "Log triage complete: 2 findings from 1 of 3 log groups (2 groups inaccessible: /aws/lambda/order-processor, /aws/ecs/inventory-service)",
  "data": { ... }
}
```

---

## Interpretation Guidance

### Rate-of-Change Analysis

The most important signal from log triage is not the absolute error count but the **rate of change** relative to the baseline:

| Ratio (current / baseline) | Interpretation | Hypothesis Weight |
|---|---|---|
| **> 10x** | Dramatic spike — almost certainly incident-related | High. Treat as primary evidence. Correlate `firstSeen` with `anomalyStartTime` from metric-baseline. |
| **3x–10x** | Significant increase — likely related to the incident | Medium-high. Strong supporting evidence, especially when combined with new patterns. |
| **1.5x–3x** | Moderate increase — may be incident-related or legitimate load growth | Medium. Include as supporting context. Look for corroboration from metric deviations. |
| **< 1.5x** | Within normal variance — unlikely to be incident-related | Low. Do not treat as primary evidence unless the pattern is new (`isNew: true`). |
| **0 baseline (new)** | Pattern never seen before — highly diagnostic | High. New errors appearing coincident with an incident are strong causal indicators. |

### New vs. Pre-Existing Findings

The `isNew` field is critical for hypothesis generation:

- **New findings** (`isNew: true`): These errors did not exist in the 7-day baseline. They are highly diagnostic — a new error pattern appearing at the time of an incident strongly suggests a causal relationship. Prioritize these in hypothesis generation.
- **Pre-existing findings** (`isNew: false`): These errors existed before the incident. They may still be relevant if their rate has increased dramatically (check count vs. baseline), but they are less diagnostic than new patterns. A pre-existing error with a 10x increase is still significant.

### Error Pattern Severity

| Severity | Patterns | Interpretation |
|----------|----------|----------------|
| **fatal** | `FATAL`, process exit, panic, segfault | Application crashed or became completely unresponsive. Immediate impact on availability. |
| **error** | `ERROR`, `Exception`, `ConnectionRefused`, `Timeout` | Application is experiencing failures but may still be partially operational. Likely causing user-facing errors. |
| **warning** | `WARN`, deprecation, retry attempts | Application is degraded but still handling requests. May indicate early signs of a developing issue. |

### Using firstSeen for Temporal Correlation

The `firstSeen` timestamp of new findings is especially valuable:

1. **Correlation with changes**: If `firstSeen` closely follows a deployment timestamp from deploy-correlation (within 5 minutes), this strongly implicates the deployment.
2. **Correlation with metric anomaly**: If `firstSeen` aligns with `anomalyStartTime` from metric-baseline, the log errors and metric deviation likely share a root cause.
3. **Cascading failures**: If multiple new findings have staggered `firstSeen` times (e.g., ConnectionRefused at T, then Timeout at T+2min), this suggests a cascading failure pattern originating from the earliest error.

### Sample Log Lines Usage

The `sampleLogLines` field (max 5 per pattern) provides:

- Stack trace fragments for exception classification
- Connection target information (hostnames, ports) for identifying failed dependencies
- Request IDs or correlation IDs for detailed investigation
- Error codes that map to specific failure modes

Use these to refine hypotheses — e.g., if sample lines show `ConnectionRefused: redis:6379`, the hypothesis should name Redis connectivity as the probable failure domain.

---

## MCP Dependencies

This skill uses:

- **cloudwatch-mcp** — for CloudWatch Logs Insights queries
  - `queryLogInsights` — runs Insights queries against specified log groups
  - Connection timeout: 30 seconds (orchestrator-enforced)
- **kubernetes-mcp** — for pod log queries (when namespaces are provided)
  - `getPods` — identifies pods in target namespaces
  - Pod log access for exception and crash pattern matching
  - Connection timeout: 30 seconds (orchestrator-enforced)

Skill execution timeout: 15 seconds (orchestrator-enforced)

---

## Error Handling

The script handles failures gracefully per Requirement 5.7:

- **Inaccessible log groups**: Skip the group, record it as unavailable, continue querying remaining groups. Only return an error response if ALL log groups are inaccessible.
- **Log Insights query timeout**: Return findings from completed queries; note timed-out groups in the message.
- **Empty results**: Return a success response with an empty findings list and `totalErrorCount: 0`. This is a valid result (no errors found is informative).
- **MCP connection failure**: Return error response with unavailability reason. The orchestrator proceeds to the next workflow phase.
- **Malformed log group names**: Reject invalid log group names in the input and return an error response indicating which names are invalid.

The orchestrator should proceed to the hypothesis-engine phase regardless of this skill's outcome.
