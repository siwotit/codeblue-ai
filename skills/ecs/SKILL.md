---
name: ecs
description: Investigate an ECS service or task problem. Use when the engineer reports task failures, circuit breaker triggers, deployment issues, service instability, container crashes, or ECS-related alarms.
---

## Investigation Steps

1. Identify the cluster and service from the engineer's input
2. Describe the service — running count vs desired, deployment state, circuit breaker status
3. Read service events — look for circuit breaker triggers, deployment failures, task placement errors
4. If tasks are failing, describe the stopped tasks — get stop codes, stopped reasons, exit codes
5. If the task definition might be the cause, describe it — check image, environment vars, resource limits, secrets
6. If a load balancer is attached, check target group health — healthy vs unhealthy targets
7. Check CloudWatch metrics for the service — CPUUtilization, MemoryUtilization, RunningTaskCount
8. Look for recent deployments or task definition changes that might have caused the issue

## Key patterns

- **runningCount < desiredCount**: Tasks failing to start or being terminated
- **Circuit breaker triggered**: Repeated launch failures — check stopped task reasons
- **Exit code 137**: OOM killed — memory limit too low or memory leak
- **Exit code 1**: Application error — check logs
- **CannotPullContainer**: Image doesn't exist, registry auth failed, or network issue
- **ResourceNotFound**: IAM role, secrets, or task definition references something missing

## What to report

- Service state (running/desired/pending)
- Root cause of task failures (with specific stop codes and reasons)
- Whether a recent deployment caused the issue
- Recommended fix (roll back, increase resources, fix config, etc.)
- What you couldn't check and what remains uncertain
