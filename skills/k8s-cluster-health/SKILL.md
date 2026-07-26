# K8s Cluster Health Skill

## Classification
**Mixed** — Balanced script + prompt guidance. The script fetches and structures cluster data via the kubernetes-mcp; the SKILL.md guides interpretation and synthesis of findings.

## Trigger Conditions

Invoke this skill when:
- The incoming `NormalizedAlert` has a **non-empty `cluster` field**
- The orchestrator enters the cluster health check phase
- An interactive triage session requests cluster health assessment

Do NOT invoke when:
- The alert has an empty or absent `cluster` field (skip K8s checks entirely)
- The alert is purely metric/log-based with no Kubernetes context

## Input Contract

The script accepts JSON via stdin with the following schema:

```json
{
  "cluster": "string (required) — EKS cluster name",
  "region": "string (required) — AWS region",
  "namespace": "string (optional) — scope to a specific namespace, omit for cluster-wide"
}
```

### Field Descriptions
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `cluster` | string | Yes | Name of the EKS cluster to inspect |
| `region` | string | Yes | AWS region where the cluster resides |
| `namespace` | string | No | If provided, scope pod and event queries to this namespace |

## Output Contract

The script returns a `SkillResponse` JSON to stdout:

```json
{
  "status": "success" | "error",
  "message": "string — empty on success, error description on failure",
  "data": {
    "clusterName": "string",
    "region": "string",
    "overallHealth": "healthy" | "degraded" | "critical",
    "nodes": {
      "total": "number",
      "ready": "number",
      "notReady": ["string — node names"],
      "conditions": [
        {
          "nodeName": "string",
          "condition": "MemoryPressure | DiskPressure | PIDPressure | NetworkUnavailable | NotReady",
          "status": "boolean",
          "since": "ISO8601",
          "message": "string"
        }
      ]
    },
    "pods": {
      "total": "number",
      "running": "number",
      "pending": "number",
      "failed": "number",
      "crashLooping": [],
      "oomKilled": []
    },
    "recentEvents": [
      {
        "type": "Warning | Normal",
        "reason": "string",
        "involvedObject": "string",
        "message": "string",
        "count": "number",
        "firstTimestamp": "ISO8601",
        "lastTimestamp": "ISO8601"
      }
    ]
  }
}
```

## Interpretation Guidance

### Overall Health Classification

The script determines `overallHealth` based on these rules:
- **critical**: Any node is NotReady OR >25% of pods are in failed/pending state
- **degraded**: Any resource pressure condition is active (MemoryPressure, DiskPressure, PIDPressure, NetworkUnavailable) OR >10% of pods are pending/failed OR crash-looping pods exist
- **healthy**: All nodes Ready, no pressure conditions, pods running normally

### Reading the Results

When interpreting the output:

1. **Node health** — Not-ready nodes are the highest priority. Check `nodes.notReady` first. Resource pressure conditions indicate impending failures even if nodes are currently Ready.

2. **Pod status** — High pending counts suggest scheduling issues (resource exhaustion, node affinity failures, taints). Failed pods with crash-looping indicate application-level problems. OOMKilled pods point to memory limits being too low or memory leaks.

3. **Recent events** — Warning events in the last hour provide temporal context. Look for:
   - `FailedScheduling` — resource exhaustion
   - `Unhealthy` — liveness/readiness probe failures
   - `BackOff` — container restart loops
   - `FailedMount` — volume or secret issues
   - `NodeNotReady` — node-level problems

4. **Correlation with alert** — If the alert mentions a specific workload, check whether the cluster-wide health explains the workload-specific symptom (e.g., node pressure causing pod evictions in the affected namespace).

### Confidence Adjustments

- If the kubernetes-mcp fails to return node data: reduce confidence by 0.3
- If event data is missing: reduce confidence by 0.1 (events are supplementary)
- If pod data is incomplete: reduce confidence by 0.2

### Downstream Skills

After cluster health is assessed:
- If pod failures are found → invoke `pod-failure-triage` for detailed classification
- If node conditions are abnormal → invoke `node-condition-check` for deeper analysis
- Results feed into the hypothesis engine as part of the incident context
