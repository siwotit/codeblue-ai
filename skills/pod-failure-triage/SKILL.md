# Pod Failure Triage Skill

## Classification

**Type**: Mixed (balanced script + prompt interpretation)

## Trigger Conditions

Invoke this skill when:

1. The `k8s-cluster-health` skill reports pod failures in the `ClusterHealthReport` (non-empty `crashLooping` or `oomKilled` lists, or `failed` pod count > 0)
2. The orchestrator has identified Kubernetes workloads in the alert's affected resources and needs detailed pod failure classification
3. An engineer requests direct pod failure investigation via interactive triage (e.g., "check pod failures in namespace X")

## Input Contract

The script accepts JSON via stdin with the following schema:

```json
{
  "cluster": "string (required) - EKS cluster name",
  "namespace": "string (optional) - specific namespace to scope investigation, omit for cluster-wide",
  "label_selector": "string (optional) - Kubernetes label selector to filter pods"
}
```

### Input Examples

Cluster-wide scan:
```json
{
  "cluster": "prod-us-east-1"
}
```

Namespace-scoped:
```json
{
  "cluster": "prod-us-east-1",
  "namespace": "payments"
}
```

With label selector:
```json
{
  "cluster": "prod-us-east-1",
  "namespace": "payments",
  "label_selector": "app=checkout-service"
}
```

## Output Contract

The script returns a `SkillResponse` JSON to stdout:

```json
{
  "status": "success",
  "message": "",
  "data": {
    "cluster": "string - cluster name",
    "total_failed_pods": "number - total count of pods in failure state",
    "failures": [
      {
        "name": "string - pod name",
        "namespace": "string - pod namespace",
        "reason": "string - one of: CrashLoopBackOff, OOMKilled, ImagePullBackOff, CreateContainerError, SandboxError",
        "restartCount": "number - container restart count",
        "lastTransition": "string - ISO 8601 timestamp of last state transition",
        "message": "string - human-readable failure message from container status"
      }
    ],
    "summary_by_reason": {
      "CrashLoopBackOff": "number",
      "OOMKilled": "number",
      "ImagePullBackOff": "number",
      "CreateContainerError": "number",
      "SandboxError": "number"
    },
    "affected_namespaces": ["string - list of namespaces with failures"]
  }
}
```

On error:
```json
{
  "status": "error",
  "message": "string - description of what went wrong",
  "data": null
}
```

## Classification Guidance

Each failed pod is classified into exactly **one** reason category based on pod/container status:

| Reason | Indicators | Typical Root Cause |
|--------|-----------|-------------------|
| **CrashLoopBackOff** | Container repeatedly exits with non-zero code, waiting state with reason "CrashLoopBackOff" | Application crash, misconfigured entrypoint, missing config/secrets |
| **OOMKilled** | Container terminated with reason "OOMKilled", exit code 137 | Memory limit too low, memory leak, unexpected traffic spike |
| **ImagePullBackOff** | Waiting state with reason "ImagePullBackOff" or "ErrImagePull" | Wrong image tag, registry auth failure, image deleted |
| **CreateContainerError** | Waiting state with reason "CreateContainerError" | Volume mount failure, security context issue, invalid resource spec |
| **SandboxError** | Pod condition shows sandbox creation failure | CNI plugin issue, node resource exhaustion, runtime failure |

### Classification Priority

When multiple container statuses are present, use this priority order:
1. OOMKilled (check terminated containers first — exit code 137 or reason "OOMKilled")
2. CrashLoopBackOff (waiting state with this reason)
3. ImagePullBackOff (waiting state with this reason or "ErrImagePull")
4. CreateContainerError (waiting state with this reason)
5. SandboxError (pod-level sandbox failure in conditions/events)

### Interpretation Guidance

- **High failure count in a single namespace**: Likely a deployment-related issue (bad image, missing config)
- **OOMKilled across multiple namespaces**: Possible node memory pressure — cross-reference with node-condition-check
- **ImagePullBackOff cluster-wide**: Possible registry connectivity issue or credential expiration
- **CrashLoopBackOff after a deployment**: Strong signal for deploy-correlation — check recent rollouts
- **SandboxError**: Often indicates node-level or CNI issues — check node conditions and VPC CNI addon status

## Notes

- This skill uses `kubernetes-mcp` to fetch pod statuses
- The script classifies each pod into exactly one failure reason
- Namespace is always reported for each failure to enable scoping
- The skill does NOT restart pods, scale deployments, or perform any remediation
- When MCP is unavailable in standalone mode, the script returns an error response
