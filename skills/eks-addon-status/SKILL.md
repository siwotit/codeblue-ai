# EKS Addon Status Skill

## Classification
**Mixed** — Balanced script + prompt guidance. The script fetches addon health data via the aws-api-mcp (EKS describe-addon); the SKILL.md guides interpretation of addon states and their impact on cluster health.

## Trigger Conditions

Invoke this skill when:
- The incoming `NormalizedAlert` has a **non-empty `cluster` field** referencing an EKS cluster
- The orchestrator enters the cluster health check phase alongside `k8s-cluster-health`
- An interactive triage session explicitly requests EKS addon health assessment
- Pod failures suggest networking or DNS issues (which may trace back to degraded addons)

Do NOT invoke when:
- The alert has an empty or absent `cluster` field (skip Kubernetes checks entirely)
- The cluster is a self-managed Kubernetes cluster (not EKS) — addons are EKS-managed components

## Input Contract

The script accepts JSON via stdin with the following schema:

```json
{
  "cluster": "string (required) — EKS cluster name",
  "region": "string (required) — AWS region where the cluster resides"
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
    "cluster": "string",
    "region": "string",
    "addons": [
      {
        "name": "string — addon name (vpc-cni, coredns, kube-proxy, aws-ebs-csi-driver)",
        "version": "string — installed addon version",
        "status": "active | degraded | failed | updating",
        "health": "string | null — health summary if available",
        "issues": ["string — issue descriptions if any"]
      }
    ],
    "degradedCount": "number — count of addons in degraded or failed state",
    "overallHealthy": "boolean — true only if all checked addons are active or updating",
    "finding": "string | null — degraded finding message when any addon is degraded/failed"
  }
}
```

## Addon Health Interpretation

### Checked Addons

The skill checks these four critical EKS-managed addons:

| Addon | Purpose | Impact When Degraded |
|-------|---------|---------------------|
| `vpc-cni` | Pod networking — assigns ENIs and IPs to pods | Pod scheduling failures, network unreachable errors, new pods cannot get IP addresses |
| `coredns` | Cluster DNS resolution | Service discovery failures, DNS timeouts, pods unable to resolve internal service names |
| `kube-proxy` | Service load balancing (iptables/IPVS rules) | Service-to-service communication failures, load balancing breaks, ClusterIP services unreachable |
| `aws-ebs-csi-driver` | Persistent volume provisioning via EBS | PVC binding failures, volume mount timeouts, StatefulSet pods stuck in Pending |

### Status Meanings

- **active**: Addon is running normally. No action needed.
- **updating**: Addon is being updated. Transient state — may cause brief disruption.
- **degraded**: Addon is partially functional. Some pods or components are unhealthy. Requires attention.
- **failed**: Addon is non-functional. Critical impact on cluster operations. Requires immediate action.

### Degraded Finding Trigger

A degraded cluster health finding is triggered when **at least one** addon reports a `degraded` or `failed` status. The finding message includes:
- Which addon(s) are affected
- Their current status
- Any issue details reported by EKS

### Reading the Results

When interpreting the output:

1. **VPC CNI degraded/failed** — Likely cause of pod scheduling failures and network connectivity issues. Check if new pods are stuck in `ContainerCreating` with IP assignment errors.

2. **CoreDNS degraded/failed** — Likely cause of DNS resolution failures across the cluster. Check for DNS timeout errors in application logs. Services may be unreachable by name.

3. **kube-proxy degraded/failed** — Likely cause of service-to-service communication failures. Check if ClusterIP services are unreachable. May manifest as connection refused or timeout errors to services.

4. **EBS CSI degraded/failed** — Likely cause of PersistentVolumeClaim (PVC) binding failures. Check for pods stuck in Pending with volume-related events. Affects StatefulSets and any workload using EBS volumes.

### Confidence Adjustments

- If the aws-api-mcp fails to return addon data for any addon: reduce confidence by 0.2 per inaccessible addon
- If all addon queries fail: reduce overall confidence by 0.4
- If addons report `updating` status: note this as transient, confidence remains neutral

### Correlation with Other Findings

- VPC CNI issues often correlate with `FailedScheduling` events and pending pods in `k8s-cluster-health`
- CoreDNS issues correlate with timeout errors in `log-triage` findings
- EBS CSI issues correlate with `FailedMount` events in the cluster events
- kube-proxy issues correlate with connection refused patterns in application logs

### Downstream Impact

Results from this skill:
- Feed into the `ClusterHealthReport.addons` field in the overall cluster health assessment
- Inform the hypothesis engine — addon degradation is a strong causal signal
- May explain pod failures identified by `pod-failure-triage`
