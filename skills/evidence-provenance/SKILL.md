# Evidence Provenance Skill

## Classification
**Mixed** — Balanced script + prompt guidance. The script generates console URLs, kubectl commands, and CloudTrail links with timestamps and query parameters; the SKILL.md guides when provenance is applicable, how to handle unresolvable claims, and interpretation of provenance gaps.

## Trigger Conditions

Invoke this skill when:
- The hypothesis engine has completed and produced ranked hypotheses with supporting evidence
- The orchestrator enters the evidence provenance phase (after hypothesis generation, before summary formatting)
- An interactive triage session requests provenance annotation for accumulated findings

Do NOT invoke when:
- No hypotheses have been generated yet (nothing to annotate)
- The workflow has been short-circuited due to all MCPs failing (only raw alert data is available — no evidence to trace)

## Input Contract

The script accepts JSON via stdin with the following schema:

```json
{
  "findings": [
    {
      "claim": "string (required) — the assertion made about the incident",
      "source": "string (required) — source system: 'cloudwatch', 'kubernetes', 'cloudtrail', 'grafana'",
      "timestamp": "string (optional) — ISO 8601 timestamp of the observation",
      "sourceDetails": {
        "metricName": "string (optional) — CloudWatch metric name",
        "metricNamespace": "string (optional) — CloudWatch metric namespace",
        "dimensions": "object (optional) — key-value pairs of CloudWatch dimensions",
        "region": "string (optional) — AWS region",
        "timeRange": {
          "start": "string — ISO 8601 start of query window",
          "end": "string — ISO 8601 end of query window"
        },
        "resourceKind": "string (optional) — K8s resource kind (pod, node, deployment, etc.)",
        "resourceName": "string (optional) — K8s resource name",
        "namespace": "string (optional) — K8s namespace",
        "clusterContext": "string (optional) — K8s cluster context name",
        "eventId": "string (optional) — CloudTrail event ID"
      }
    }
  ],
  "region": "string (required) — default AWS region for URL generation",
  "accountId": "string (optional) — AWS account ID for console URLs"
}
```

### Field Descriptions

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `findings` | array | Yes | List of Finding objects with claims to annotate with provenance |
| `findings[].claim` | string | Yes | The assertion or observation to trace back to source data |
| `findings[].source` | string | Yes | Source system identifier: `cloudwatch`, `kubernetes`, `cloudtrail`, or `grafana` |
| `findings[].timestamp` | string | No | ISO 8601 timestamp when the observation was made |
| `findings[].sourceDetails` | object | No | Additional details needed to construct the provenance link |
| `findings[].sourceDetails.metricName` | string | No | CloudWatch metric name (required for CloudWatch sources) |
| `findings[].sourceDetails.metricNamespace` | string | No | CloudWatch metric namespace (required for CloudWatch sources) |
| `findings[].sourceDetails.dimensions` | object | No | CloudWatch metric dimensions as key-value pairs |
| `findings[].sourceDetails.region` | string | No | AWS region override (falls back to top-level `region`) |
| `findings[].sourceDetails.timeRange` | object | No | Query time range with ISO 8601 `start` and `end` |
| `findings[].sourceDetails.resourceKind` | string | No | Kubernetes resource kind (required for K8s sources) |
| `findings[].sourceDetails.resourceName` | string | No | Kubernetes resource name (required for K8s sources) |
| `findings[].sourceDetails.namespace` | string | No | Kubernetes namespace (required for K8s sources) |
| `findings[].sourceDetails.clusterContext` | string | No | Kubernetes cluster context (required for K8s sources) |
| `findings[].sourceDetails.eventId` | string | No | CloudTrail event ID (required for CloudTrail sources) |
| `region` | string | Yes | Default AWS region for generating console URLs |
| `accountId` | string | No | AWS account ID for scoping console URLs |

## Output Contract

The script returns a `SkillResponse` JSON to stdout:

```json
{
  "status": "success" | "error",
  "message": "string — empty on success, error description on failure",
  "data": {
    "annotatedFindings": [
      {
        "claim": "string — original claim text",
        "source": "string — source system identifier",
        "timestamp": "string — ISO 8601 timestamp of the observation",
        "provenanceResolved": "boolean — whether provenance was successfully attached",
        "consoleUrl": "string | null — deep link to the source data in a console UI",
        "verificationCommand": "string | null — executable command to reproduce the observation",
        "queryParameters": {
          "timeRange": {
            "start": "string — ISO 8601",
            "end": "string — ISO 8601"
          },
          "filters": "object — key-value pairs of query parameters used"
        },
        "unavailabilityReason": "string | null — reason provenance could not be resolved (only when provenanceResolved is false)"
      }
    ],
    "summary": {
      "totalFindings": "number",
      "resolvedCount": "number",
      "unresolvedCount": "number"
    }
  }
}
```

### ProvenanceAnnotatedFinding Schema

| Field | Type | Description |
|-------|------|-------------|
| `claim` | string | The original claim text, unchanged |
| `source` | string | Source system (`cloudwatch`, `kubernetes`, `cloudtrail`, `grafana`) |
| `timestamp` | string | ISO 8601 timestamp of the original observation |
| `provenanceResolved` | boolean | `true` if a console URL or verification command was successfully generated |
| `consoleUrl` | string \| null | Deep link URL to the source data; null if provenance unresolved |
| `verificationCommand` | string \| null | Executable command (e.g., kubectl) to reproduce the state; null if not applicable |
| `queryParameters` | object | The time range and filters used during the original query, for reproducibility |
| `queryParameters.timeRange` | object | `start` and `end` in ISO 8601 format |
| `queryParameters.filters` | object | Key-value pairs of dimension/label filters applied |
| `unavailabilityReason` | string \| null | Explanation of why provenance could not be resolved; null when resolved |

## URL Generation Patterns

### CloudWatch Console Deep Links

Generate metric graph deep links with pre-populated parameters:

```
https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#metricsV2:graph=~(
  metrics~(~(~'{namespace}~'{metricName}~'{dimKey1}~'{dimValue1}~'{dimKey2}~'{dimValue2}))
  ~view~'timeSeries
  ~stacked~false
  ~region~'{region}
  ~start~'{startISO}
  ~end~'{endISO}
  ~period~300
)
```

**Required fields**: `metricName`, `metricNamespace`, `region`, `timeRange.start`, `timeRange.end`
**Optional fields**: `dimensions` (each key-value pair becomes a dimension selector)

### CloudTrail Console Detail Page Links

Generate direct links to specific CloudTrail events:

```
https://{region}.console.aws.amazon.com/cloudtrailv2/home?region={region}#/events/{eventId}
```

**Required fields**: `eventId`, `region`

### kubectl Verification Commands

Generate commands that reproduce the observed Kubernetes state:

```bash
kubectl get {resourceKind} {resourceName} -n {namespace} --context {clusterContext} -o yaml
kubectl describe {resourceKind} {resourceName} -n {namespace} --context {clusterContext}
kubectl logs {podName} -n {namespace} --context {clusterContext} --since=1h
kubectl get events -n {namespace} --context {clusterContext} --sort-by='.lastTimestamp'
```

**Required fields**: `resourceKind`, `resourceName`, `namespace`, `clusterContext`

Command selection logic:
- Pods: use `kubectl describe` (shows events, conditions, container status)
- Deployments/ReplicaSets: use `kubectl get -o yaml` (shows rollout state)
- Events: use `kubectl get events` with time-based sorting
- Logs: use `kubectl logs --since=1h` for pod log evidence

## Interpretation Guidance

### Provenance Resolution Priority

When constructing provenance links:

1. **Console URL preferred** — Always generate a console URL when the source system has a web interface (CloudWatch, CloudTrail). Console links allow non-CLI users to verify.
2. **Verification command as complement** — For Kubernetes sources, always include a kubectl command. For CloudWatch sources, optionally include an AWS CLI command as a secondary verification method.
3. **Both when possible** — For CloudTrail events, generate both the console URL and an `aws cloudtrail lookup-events` CLI command.

### Timestamp Validation

The script validates that evidence timestamps fall within declared query time ranges:
- If `findings[].timestamp` is outside `findings[].sourceDetails.timeRange`, the finding is flagged with `provenanceResolved: false` and `unavailabilityReason: "Evidence timestamp falls outside declared query time range"`
- This guards against stale or misattributed evidence being presented as current

### Handling Unresolvable Claims

Claims cannot be resolved when:
- **Missing source details**: Required fields for the source type are absent (e.g., no `metricName` for a CloudWatch source) → reason: "Insufficient source details to generate provenance link: missing {fieldName}"
- **Unknown source type**: The `source` field is not one of the recognized systems → reason: "Unrecognized source system: {source}"
- **Timestamp outside range**: The observation timestamp is not within the declared query window → reason: "Evidence timestamp {ts} falls outside declared query time range [{start}, {end}]"
- **Missing time range**: No time range is declared for a time-sensitive source → reason: "No query time range declared for time-sensitive source"

### Confidence Adjustments

- If >20% of findings have unresolved provenance: reduce downstream confidence by 0.1
- If >50% are unresolved: reduce confidence by 0.2 and flag to the hypothesis engine that evidence traceability is weak
- Unresolved provenance does NOT remove the claim from the report — it is annotated but retained

### Downstream Usage

After provenance annotation:
- The `incident-summary-format` skill uses `consoleUrl` fields to populate evidence links in the Slack message
- The `verificationCommand` fields become the "Next Steps" section content
- Claims with `provenanceResolved: false` are still included but marked with a ⚠️ indicator in the Slack output

## Error Handling

- If the input JSON is malformed or missing `findings` array: return `status: "error"` with message describing the parsing failure
- If individual findings fail provenance resolution: annotate them as unresolved but continue processing remaining findings (never fail the entire batch)
- If the `region` field is missing: return `status: "error"` with message "Required field 'region' is missing"
