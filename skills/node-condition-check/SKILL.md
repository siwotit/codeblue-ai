# Node Condition Check Skill

## Classification
**Mixed** — Balanced script + prompt guidance. The script fetches node status data via the kubernetes-mcp and detects condition problems; the SKILL.md guides interpretation and severity assessment of findings.

## Trigger Conditions

Invoke this skill when:
- The `k8s-cluster-health` skill reports **node conditions** that are abnormal (any pressure condition active or nodes NotReady)
- The orchestrator identifies node-level degradation in the cluster health phase
- An interactive triage session requests deeper node condition analysis

Do NOT invoke when:
- The alert has an empty or absent `cluster` field (no Kubernetes context)
- Cluster health shows all nodes Ready with no pressure conditions (no further node analysis needed)
- The issue is purely pod-level (use `pod-failure-triage` instead)

## Input Contract

The script accepts JSON via stdin with the following schema:

```json
{
  "cluster": "string (required) — EKS cluster name",
  "region": "string (required) — AWS region"
}
```

### Field Descriptions
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `cluster` | string | Yes | Name of the EKS cluster to inspect |
| `region` | string | Yes | AWS region where the cluster resides |

## Output Contract

The script returns a `SkillResponse` JSON to stdout:

```json
{
  "status": "success" | "error",
  "message": "string — empty on success, error description on failure",
  "data": {
    "clusterName": "string",
    "region": "string",
    "totalNodes": "number",
    "healthyNodes": "number",
    "conditions": [
      {
        "nodeName": "string",
        "condition": "MemoryPressure | DiskPressure | PIDPressure | NetworkUnavailable | NotReady",
        "status": true,
        "since": "ISO8601",
        "message": "string"
      }
    ],
    "summary": {
      "notReadyCount": "number",
      "memoryPressureCount": "number",
      "diskPressureCount": "number",
      "pidPressureCount": "number",
      "networkUnavailableCount": "number"
    }
  }
}
```

## Condition Severity Guidance

### Severity Ranking (highest to lowest)

1. **NotReady** — The node is completely unable to schedule or run pods. This is the most severe condition and often indicates kubelet failure, network partition, or node crash.
2. **NetworkUnavailable** — The node cannot communicate on the cluster network. Pods on this node are effectively unreachable. Often precedes or accompanies NotReady.
3. **MemoryPressure** — The node is running low on memory. The kubelet will begin evicting pods to reclaim memory. Can cascade to OOMKilled containers.
4. **DiskPressure** — The node is running low on disk space. The kubelet will evict pods and may refuse new scheduling. Often caused by excessive logging or container image accumulation.
5. **PIDPressure** — The node is running low on available process IDs. Rare but critical when it occurs — indicates fork bombs, runaway process creation, or extremely high container density.

### Interpreting Results

When analyzing node conditions:

1. **NotReady nodes** are the highest priority. A node that is NotReady means all workloads on it are impacted. Check if the node was recently drained, terminated, or experienced a kubelet crash.

2. **Multiple conditions on the same node** compound the severity. A node with both MemoryPressure and DiskPressure is likely in a death spiral that will soon become NotReady.

3. **Condition duration matters** — Check the `since` timestamp. A condition active for minutes may be transient; one active for hours indicates a persistent problem requiring intervention.

4. **Blast radius estimation** — Count affected nodes relative to total. If >50% of nodes have conditions, the cluster itself is at risk. If a single node has conditions, the impact is limited to workloads scheduled on that node.

5. **Correlation with other signals**:
   - MemoryPressure + OOMKilled pods → memory limit misconfiguration or leak
   - DiskPressure + FailedMount events → storage subsystem problem
   - NetworkUnavailable + connection timeouts → CNI or underlying network issue
   - NotReady + recent scaling events → possible instance launch failure

### Confidence Adjustments

- If the kubernetes-mcp fails to return node data: set confidence to 0.1 (no meaningful analysis possible)
- If only partial node data is returned: reduce confidence by 0.2 per missing node
- If condition timestamps are missing: reduce confidence by 0.1 (cannot assess duration)

### Downstream Usage

Results from this skill:
- Feed into the hypothesis engine as node-level evidence
- Influence blast radius calculations (affected nodes → affected pods)
- May trigger escalation if multiple nodes are NotReady
- Provide provenance for claims about node health in the incident summary
