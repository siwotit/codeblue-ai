# ECS Triage Skill

## Classification
**Mixed** — Balanced script + prompt guidance.

## Purpose

Consolidated ECS service/task diagnosis skill. Runs all ECS checks in a single invocation:
- Service stability (running count vs desired, deployment status, circuit breaker state)
- Task failure classification (CannotPullContainer, ResourceNotFound, OOM, exit codes)
- Load balancer target health (if ALB/NLB attached)
- Recent deployments and task definition changes from CloudTrail
- Container Insights metrics (CPU, memory, network for tasks/services)

## Trigger Conditions

Invoke this skill when:
- The incoming `NormalizedAlert` references an ECS service or cluster (ARN contains `:service/` or `:cluster/` in ECS context)
- Alert metric namespace is `AWS/ECS` or `ECS/ContainerInsights`
- An interactive triage session references ECS services or tasks

Do NOT invoke when:
- The alert is EKS/K8s (use `eks-triage`)
- The alert is bare EC2 with no ECS context (use `ec2-triage`)

## Input Contract

```json
{
  "cluster": "string (required) — ECS cluster name or ARN",
  "service": "string (optional) — ECS service name to focus on",
  "region": "string (required) — AWS region",
  "account": "string (optional) — AWS account ID"
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
    "services": [
      {
        "serviceName": "string",
        "status": "ACTIVE | DRAINING | INACTIVE",
        "runningCount": "number",
        "desiredCount": "number",
        "pendingCount": "number",
        "deployments": [
          {
            "id": "string",
            "status": "PRIMARY | ACTIVE | INACTIVE",
            "taskDefinition": "string",
            "runningCount": "number",
            "desiredCount": "number",
            "rolloutState": "COMPLETED | IN_PROGRESS | FAILED",
            "createdAt": "ISO8601"
          }
        ],
        "circuitBreaker": {
          "enabled": "boolean",
          "rollback": "boolean",
          "status": "string | null"
        },
        "loadBalancer": {
          "targetGroupArn": "string | null",
          "healthyCount": "number",
          "unhealthyCount": "number"
        }
      }
    ],
    "failedTasks": [
      {
        "taskArn": "string",
        "stopCode": "string (TaskFailedToStart | EssentialContainerExited | ...)",
        "stoppedReason": "string",
        "exitCode": "number | null",
        "container": "string",
        "stoppedAt": "ISO8601"
      }
    ],
    "recentChanges": [
      {
        "eventName": "string",
        "timestamp": "ISO8601",
        "actor": "string",
        "description": "string"
      }
    ],
    "findings": ["string — human-readable findings"]
  }
}
```

## Interpretation Guidance

### Service Stability
- **runningCount < desiredCount**: Tasks are failing to start or being terminated
- **Multiple deployments active**: A rolling update is in progress (or stuck)
- **rolloutState = FAILED**: The deployment failed and circuit breaker may have rolled back
- **Circuit breaker triggered**: Indicates repeated task launch failures — look at `failedTasks`

### Task Failure Classification
| Stop Code | Typical Root Cause |
|-----------|-------------------|
| TaskFailedToStart | Container image pull failure, resource constraints, IAM permissions |
| EssentialContainerExited | Application crash (check exit code), OOM killed (exit 137) |
| ServiceSchedulerInitiated | Service scaling down or rebalancing |
| SpotInterruption | Fargate Spot capacity reclaimed |
| UserInitiated | Manual stop by user/automation |

### Exit Codes
| Code | Meaning |
|------|---------|
| 0 | Clean exit (normal shutdown) |
| 1 | Application error |
| 137 | SIGKILL / OOM killed |
| 139 | SIGSEGV (segfault) |
| 143 | SIGTERM (graceful shutdown requested) |

### Recent Changes (Deploy Correlation)
CloudTrail events in the last 24h:
- `UpdateService` — deployment trigger (new task def, desired count, etc.)
- `RegisterTaskDefinition` — new task definition revision
- `CreateService` / `DeleteService` — service lifecycle
- `StopTask` — manual task termination
- `PutClusterCapacityProviders` — capacity provider changes
- `UpdateClusterSettings` — cluster configuration changes

### Load Balancer Health
If the service has an attached target group:
- **All unhealthy targets**: Application not responding to health checks (port/path mismatch, crash)
- **Mixed healthy/unhealthy**: Rolling deployment in progress or partial failure
- **Draining targets**: Tasks being replaced (normal during deployment)

### Confidence Adjustments
- ECS API fails for describe-services: reduce confidence by 0.3
- CloudWatch/Container Insights unavailable: reduce by 0.2
- CloudTrail query fails: reduce by 0.1
- Load balancer health unavailable: reduce by 0.1
