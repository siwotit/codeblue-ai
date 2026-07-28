# EKS Triage Skill

## Classification
**Mixed** — Balanced script + prompt guidance. The script fetches and structures cluster data via kubernetes-mcp and aws-api-mcp; this document guides interpretation.

## Purpose

Consolidated EKS/Kubernetes diagnosis skill. Runs all K8s checks in a single invocation:
- Cluster health (node readiness, pod status, recent warning events)
- Pod failure classification (CrashLoopBackOff, OOMKilled, ImagePullBackOff, etc.)
- Node condition detection (MemoryPressure, DiskPressure, PIDPressure, NetworkUnavailable)
- EKS addon health (VPC CNI, CoreDNS, kube-proxy, EBS CSI driver)
- Recent K8s rollouts/scaling events (deploy-correlation for K8s domain)

## Trigger Conditions

Invoke this skill when:
- The incoming `NormalizedAlert` has a **non-empty `cluster` field**
- An interactive triage session requests EKS/K8s health assessment
- Pod or node failures are suspected

Do NOT invoke when:
- The alert has an empty or absent `cluster` field
- The issue is purely CloudWatch metrics with no K8s context

## Input Contract

```json
{
  "cluster": "string (required) — EKS cluster name",
  "region": "string (required) — AWS region",
  "namespace": "string (optional) — scope to a specific namespace"
}
```

## Output Contract

```json
{
  "status": "success" | "error",
  "message": "string",
  "data": {
    "cluster": "string",
    "region": "string",
    "clusterHealth": {
      "clusterName": "string",
      "region": "string",
      "overallHealth": "healthy | degraded | critical",
      "nodes": { "total", "ready", "notReady", "conditions" },
      "pods": { "total", "running", "pending", "failed", "crashLooping", "oomKilled" },
      "recentEvents": [...]
    },
    "podFailures": {
      "cluster": "string",
      "total_failed_pods": "number",
      "failures": [...],
      "summary_by_reason": {...},
      "affected_namespaces": [...]
    },
    "addonHealth": {
      "cluster": "string",
      "addons": [...],
      "degradedCount": "number",
      "overallHealthy": "boolean",
      "finding": "string | null"
    },
    "recentRollouts": [
      {
        "reason": "string",
        "kind": "string",
        "name": "string",
        "namespace": "string",
        "timestamp": "ISO8601",
        "message": "string"
      }
    ]
  }
}
```

## Interpretation Guidance

### Overall Health Classification
- **critical**: Any node NotReady OR >25% pods failed/pending
- **degraded**: Resource pressure, >10% pods failing, crash-looping pods, or addon degradation
- **healthy**: All systems nominal

### Pod Failure Categories
| Reason | Typical Root Cause |
|--------|-------------------|
| CrashLoopBackOff | App crash, misconfigured entrypoint, missing config/secrets |
| OOMKilled | Memory limit too low, memory leak, traffic spike |
| ImagePullBackOff | Wrong tag, registry auth failure, image deleted |
| CreateContainerError | Volume mount failure, security context issue |
| SandboxError | CNI plugin issue, node resource exhaustion |

### Addon Impact
| Addon | Impact When Degraded |
|-------|---------------------|
| vpc-cni | Pod scheduling failures, no IP addresses |
| coredns | Service discovery failures, DNS timeouts |
| kube-proxy | Service-to-service communication breaks |
| aws-ebs-csi-driver | PVC binding failures, volume mount timeouts |

### Rollout Correlation
Recent rollouts found in `recentRollouts` should be correlated with the anomaly start time. A deployment within minutes of the incident is a strong causal signal.

### Confidence Adjustments
- kubernetes-mcp fails for nodes: reduce confidence by 0.3
- Event data missing: reduce by 0.1
- Pod data incomplete: reduce by 0.2
- aws-api-mcp fails for addons: reduce by 0.2
