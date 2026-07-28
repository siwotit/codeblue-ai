# EC2 Triage Skill

## Classification
**Mixed** — Balanced script + prompt guidance.

## Purpose

Consolidated EC2 instance diagnosis skill. Runs all EC2 checks in a single invocation:
- Instance status checks (system status, instance status, impaired/degraded)
- Instance health metrics (CPU, network, disk, status check failures)
- Security group and networking state
- Recent instance changes from CloudTrail (stop/start/terminate/modify, recent AMI changes, EBS attach/detach)
- Auto Scaling group health (if instance belongs to an ASG)

## Trigger Conditions

Invoke this skill when:
- The incoming `NormalizedAlert` references an EC2 instance (ARN contains `:instance/` or dimension is `InstanceId`)
- Alert metric namespace is `AWS/EC2`
- An interactive triage session references EC2 instances

Do NOT invoke when:
- The alert is purely EKS/K8s (use `eks-triage` instead)
- The alert is ECS task-level (use `ecs-triage` instead)
- No EC2 instance can be identified from the alert

## Input Contract

```json
{
  "instanceIds": ["string (required) — one or more EC2 instance IDs"],
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
    "region": "string",
    "instances": [
      {
        "instanceId": "string",
        "state": "running | stopped | terminated | ...",
        "statusChecks": {
          "system": "ok | impaired | initializing | insufficient-data",
          "instance": "ok | impaired | initializing | insufficient-data"
        },
        "metrics": {
          "cpuUtilization": "number (%, last 5min)",
          "networkIn": "number (bytes, last 5min)",
          "networkOut": "number (bytes, last 5min)",
          "statusCheckFailed": "number (0 or 1)"
        },
        "securityGroups": [{"id": "string", "name": "string"}],
        "subnetId": "string",
        "vpcId": "string",
        "autoScalingGroup": "string | null",
        "launchTime": "ISO8601"
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

### Status Check Failures
- **System status impaired**: Hardware or infrastructure problem on the host. Customer cannot fix — requires instance stop/start (migrates to new host) or AWS intervention.
- **Instance status impaired**: Software-level problem on the instance. Likely OS-level issue (kernel panic, network misconfiguration, disk full).
- Both impaired simultaneously: Usually a host failure.

### Key Metrics
| Metric | Concern When |
|--------|-------------|
| CPUUtilization | >95% sustained — compute-bound, possible runaway process |
| StatusCheckFailed_System | >0 — host hardware problem |
| StatusCheckFailed_Instance | >0 — guest OS problem |
| NetworkIn/Out drops to 0 | Network connectivity loss |

### Recent Changes (Deploy Correlation)
CloudTrail events in the last 24h that matter:
- `StopInstances` / `StartInstances` / `RebootInstances` — operational changes
- `ModifyInstanceAttribute` — security group, instance type, or EBS changes
- `RunInstances` — new instance launched (relevant for ASG scaling)
- `TerminateInstances` — capacity reduction
- `AttachVolume` / `DetachVolume` — storage changes
- `AuthorizeSecurityGroupIngress/Egress` — network rule changes

### Confidence Adjustments
- aws-api-mcp fails for describe-instances: reduce confidence by 0.3
- CloudWatch metrics unavailable: reduce by 0.2
- CloudTrail query fails: reduce by 0.1
