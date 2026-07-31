---
name: ecs
description: Investigate ECS service and task problems. Use when the engineer asks about task failures, deployments, circuit breaker, service instability, container crashes, scaling, load balancer health, or ECS-related alarms.
---

# ECS Investigation

## Decision Tree

```
Problem received → What type?
│
├─ "Tasks keep failing" / "Service won't stabilize"
│  → Describe the service (ecs:DescribeServices) — running vs desired count, deployment state
│  → Read service events (last 100) — look for error messages, circuit breaker triggers
│  → List stopped tasks (ecs:ListTasks with desiredStatus=STOPPED)
│  → Describe stopped tasks (ecs:DescribeTasks) — get stopCode, stoppedReason, container exit codes
│  → Classify the failure (see Task Failure Classification below)
│  → If image issue: check ECR or registry accessibility
│  → If resource issue: check cluster capacity (CPU/memory reservations vs available)
│
├─ "Deployment is stuck" / "Rolling update not completing"
│  → Describe the service — check deployments array
│  → Look for multiple active deployments (PRIMARY + ACTIVE = rolling update in progress)
│  → Check rolloutState on each deployment (COMPLETED / IN_PROGRESS / FAILED)
│  → Check circuit breaker configuration and state
│  → If circuit breaker triggered: examine the stopped tasks from the new deployment
│  → Check minimumHealthyPercent and maximumPercent settings
│  → If tasks from new deployment keep dying: the new task definition has a problem
│  → Compare old vs new task definition (what changed?)
│
├─ "Container keeps crashing" / "Exit code non-zero"
│  → Describe stopped tasks — get container exit codes and reasons
│  → Check CloudWatch Logs for the task's log group (error messages, stack traces, OOM)
│  → Exit code 137 = OOM killed (container exceeded memory limit)
│  → Exit code 1 = application error (check logs)
│  → Exit code 139 = segfault
│  → Exit code 143 = SIGTERM (graceful shutdown — normal during deployment)
│  → For other exit codes, reference: https://repost.aws/knowledge-center/ecs-task-stopped
│  → Check task definition resource limits (is memory/CPU too low?)
│  → Check if the container image exists and is pullable
│
├─ "Health checks failing" / "Targets unhealthy"
│  → Identify the target group attached to the service
│  → Describe target health (elasticloadbalancing:DescribeTargetHealth)
│  → Check health check configuration (path, port, interval, thresholds)
│  → Common causes:
│    - App not listening on the health check port (wrong containerPort mapping)
│    - Health check path returns non-200 (app not ready, missing endpoint)
│    - Health check timeout too short for app startup time
│    - Security group doesn't allow traffic from ALB to container port
│    - Timeout mismatch (see Timeout Chain below)
│  → Check CloudWatch metrics: HealthyHostCount, UnHealthyHostCount, TargetResponseTime
│  → Check if new deployment is in progress (targets in draining state is normal)
│
├─ "Service can't scale" / "Not enough capacity"
│  → Check cluster capacity providers (Fargate, EC2, Fargate Spot)
│  → For EC2 launch type: check instance count, CPU/memory reservations vs registered
│  → For Fargate: check if hitting account-level task limits
│  → Look for "service was unable to place a task" in service events
│  → Check if subnets have available IPs (awsvpc mode needs one IP per task)
│  → Check scaling policies and recent scaling activity
│
├─ "What changed?" / "Service was fine yesterday"
│  → Check CloudTrail for recent ECS events:
│    - UpdateService (new task definition, desired count change, config change)
│    - RegisterTaskDefinition (new revision)
│    - CreateService / DeleteService
│    - PutClusterCapacityProviders
│  → Compare current task definition with previous revision (what's different?)
│  → Check service events timeline — when did problems start?
│  → Correlate with deployment times
│
├─ "Task takes too long to start" / slow startup
│  → Check if image pull is slow (large image or cross-region ECR)
│  → Check if secrets/parameters retrieval is timing out
│  → Check if ENI attachment is slow (awsvpc mode in subnet with few IPs)
│  → Check health check grace period — is it long enough for the app to start?
│  → Check container dependency ordering (dependsOn in task definition)
│
└─ "I want to understand this service" / general service questions
   → Describe the service (launch type, task definition, load balancer, scaling)
   → Describe the task definition (containers, resources, env vars, secrets, volumes)
   → Check CloudWatch metrics (CPUUtilization, MemoryUtilization, RunningTaskCount)
   → Check service events for recent activity
```

## Task Failure Classification

When tasks stop, the `stopCode` and `stoppedReason` tell you why:

| stopCode | Meaning | Investigation |
|----------|---------|---------------|
| TaskFailedToStart | Container couldn't launch | Check stoppedReason for details (image pull, resource, IAM) |
| EssentialContainerExited | A container marked essential crashed | Check exit code and logs |
| ServiceSchedulerInitiated | Service scaling down or rebalancing | Normal — not a failure |
| SpotInterruption | Fargate Spot capacity reclaimed | Expected with Spot — retry on standard Fargate |
| UserInitiated | Manually stopped | Check CloudTrail for who stopped it |

### Common stoppedReason values

| Reason | Root cause | Fix |
|--------|-----------|-----|
| `CannotPullContainerError` | Image not found, auth failed, network issue | Check image URI, ECR permissions, VPC endpoints |
| `ResourceNotFoundException` | Secret, parameter, or IAM role doesn't exist | Check task execution role permissions and secret ARNs |
| `OutOfMemoryError` | Container exceeded hard memory limit | Increase memory in task definition |
| `CannotStartContainerError` | Entrypoint/command failed | Check container command and entrypoint in task def |
| `TaskFailedElbHealthChecks` | Target never became healthy | Check health check config, app startup time, port mapping |

## Key Patterns

### Circuit Breaker

ECS deployment circuit breaker protects against bad deployments. When triggered:
1. New tasks keep failing to stabilize
2. After threshold failures, ECS automatically rolls back to the last working deployment
3. Service events show "circuit breaker: rolling back deployment"

**To investigate:** Look at the stopped tasks from the failed deployment. The reason they failed IS the reason the circuit breaker triggered.

### Service Events

Service events (visible in ecs:DescribeServices `events` field) are the most valuable diagnostic. Look for:
- `has reached a steady state` — service is healthy
- `was unable to place a task because no container instance met all of its requirements` — capacity issue
- `registered x targets in target group` — deployment progress
- `has begun draining connections` — old tasks being replaced
- `circuit breaker: rolling back` — deployment failed
- `is unable to consistently start tasks successfully` — repeated launch failures

### Fargate vs EC2 Launch Type

| Issue | Fargate | EC2 |
|-------|---------|-----|
| No capacity | Check account task limits, subnet IPs | Check instance count, registered CPU/memory |
| Networking | awsvpc always (each task gets ENI) | Depends on network mode (bridge/awsvpc/host) |
| Logs | Always CloudWatch (configured in task def) | Depends on log driver configuration |
| Instance issues | N/A (AWS manages infra) | Check underlying EC2 instances (use ec2 skill) |

### Timeout Chain (ALB ↔ App)

A common source of 502s and 504s is mismatched timeout values between the ALB and the application:

```
Client → ALB (idle timeout) → Target/Container (app timeout / keep-alive)
```

**The rule:** App keep-alive timeout MUST be greater than ALB idle timeout.

| Symptom | Cause | Fix |
|---------|-------|-----|
| Intermittent 502 | App's keep-alive timeout < ALB idle timeout. App closes the connection; ALB sends a request on it at the same instant. | Increase app keep-alive to be higher than ALB idle timeout (default 60s, so app should be ≥65s) |
| 504 Gateway Timeout | ALB idle timeout < app request processing time. ALB gives up before the backend finishes. | Increase ALB idle timeout to match longest expected request duration |
| 502 during deployments | Old tasks draining but ALB still routes to them | Check deregistration delay — should be long enough for in-flight requests to complete |

**How to check:**
- ALB idle timeout: `elasticloadbalancing:DescribeLoadBalancerAttributes` → `idle_timeout.timeout_seconds` (default 60s)
- App keep-alive: depends on the framework (e.g., Node.js `server.keepAliveTimeout`, nginx `keepalive_timeout`, Java `server.connection-timeout`)
- Deregistration delay: target group attribute `deregistration_delay.timeout_seconds` (default 300s)

**Correct ordering:**
```
App keep-alive (e.g. 65s) > ALB idle timeout (e.g. 60s) > Health check interval
Deregistration delay > Longest in-flight request duration
```

### Task Definition Diff

When a deployment breaks things, the fix is usually in the task definition diff. Key fields to compare between revisions:
- `image` — wrong tag, doesn't exist, or points to wrong environment
- `memory` / `cpu` — too low for the new version
- `environment` / `secrets` — missing or changed env var
- `command` / `entryPoint` — changed startup command
- `executionRoleArn` / `taskRoleArn` — wrong IAM role (see IAM Roles below)
- `portMappings` — changed port but ALB health check still points to old port
- `networkMode` — changed from bridge to awsvpc or vice versa (see Task Networking below)

### IAM Roles: Execution Role vs Task Role

ECS tasks use TWO different IAM roles. Confusing them is a common source of permission errors:

| Role | Field | Used by | Purpose |
|------|-------|---------|---------|
| **Execution role** | `executionRoleArn` | ECS agent (infrastructure) | Pull images from ECR, fetch secrets from Secrets Manager/SSM Parameter Store, write logs to CloudWatch |
| **Task role** | `taskRoleArn` | Your application code | Call AWS APIs from inside the container (S3, DynamoDB, SQS, etc.) |

**Common failures:**

| Error | Which role is wrong | Fix |
|-------|--------------------|----|
| `CannotPullContainerError: pull image manifest` | Execution role — missing `ecr:GetDownloadUrlForLayer`, `ecr:BatchGetImage` | Add ECR read permissions to execution role |
| `ResourceNotFoundException: Secrets Manager` | Execution role — missing `secretsmanager:GetSecretValue` | Add Secrets Manager read to execution role |
| `AccessDenied` on S3/DynamoDB/SQS from app code | Task role — missing the service permission | Add the required permission to the task role |
| `Unable to write logs to CloudWatch` | Execution role — missing `logs:CreateLogStream`, `logs:PutLogEvents` | Add CloudWatch Logs write to execution role |

**Key insight:** If the task fails to START (image pull, secret fetch), it's the execution role. If the task starts but the APPLICATION gets access denied, it's the task role.

### Task Networking

| Network mode | How it works | When to use |
|--------------|-------------|-------------|
| `awsvpc` | Each task gets its own ENI with private IP | Fargate (required), or EC2 when you need per-task SGs |
| `bridge` | Tasks share the host's network via port mapping | EC2 launch type, legacy apps |
| `host` | Tasks use the host's network directly (no port mapping) | EC2, high-performance networking (one task per port per host) |

**awsvpc common issues:**
- **Subnet IP exhaustion**: Each task needs one IP. Small subnets run out fast. Check available IPs in the subnet.
- **ENI limits on EC2**: Each EC2 instance has a max ENI count (varies by type). More tasks than ENI slots = placement failure.
- **Security groups**: In awsvpc, the task's SG is separate from the instance SG. Traffic must be allowed in BOTH if coming from outside.
- **No public IP by default**: Fargate tasks in awsvpc get no public IP unless `assignPublicIp: ENABLED` or they're in a private subnet with NAT gateway. Without internet access, they can't pull images from public registries.

**"Task can't reach the internet":**
1. Is the task in a public subnet with `assignPublicIp: ENABLED`? If no → needs NAT gateway in a private subnet.
2. Is there a NAT gateway in the VPC? Check route table for the task's subnet → `0.0.0.0/0` should point to a NAT gateway (for private) or internet gateway (for public).
3. Does the task's security group allow outbound traffic? Check outbound rules (default allows all, but someone may have restricted it).
4. Are there VPC endpoints for the services it needs? (ECR, S3, Secrets Manager, SSM, CloudWatch Logs — all support VPC endpoints as an alternative to internet access.)

**"Task can't fetch secrets/parameters" (SSM/Secrets Manager):**
1. **Execution role permission**: Does `executionRoleArn` have `ssm:GetParameters` and/or `secretsmanager:GetSecretValue`? (See IAM Roles section above.)
2. **Network path**: Can the task reach the SSM/Secrets Manager endpoint?
   - In private subnet without NAT: needs VPC endpoint for `com.amazonaws.<region>.ssm` and/or `com.amazonaws.<region>.secretsmanager`
   - Check task SG allows outbound HTTPS (port 443) to the endpoint
3. **KMS decrypt**: If the secret is encrypted with a custom KMS key, the execution role also needs `kms:Decrypt` on that key.
4. **Secret ARN format**: Check the `secrets` field in the task definition — the ARN must be exact (including the version/stage suffix for Secrets Manager).
5. **VPC endpoint policy**: If using a VPC endpoint, its policy must allow the execution role to access the specific secrets.

**bridge mode common issues:**
- **Port conflicts**: Two tasks can't map to the same host port. Use dynamic port mapping (hostPort: 0) with ALB.
- **Security group applies at instance level**, not task level. All tasks on the same instance share one SG.

### Service Connect (Service-to-Service Communication)

ECS Service Connect enables service-to-service communication using logical names instead of IP addresses. When an engineer reports "service A can't reach service B" or "inter-service calls are failing":

**How it works:**
- Each service with Service Connect gets an Envoy proxy sidecar container injected automatically
- Services reference each other by name (e.g., `http://payment-service:8080`) not by IP
- Configured via a Cloud Map namespace (the service registry)

**Investigation steps for Service Connect issues:**
1. Check if both services are in the same Service Connect namespace
2. Describe the service — check `serviceConnectConfiguration` (is it enabled? correct namespace?)
3. Check the Envoy proxy container health — it's injected as a sidecar; if it crashes, all networking fails
4. Check CloudWatch metrics for Service Connect:
   - `ActiveConnectionCount`, `NewConnectionCount` (namespace: `AWS/ECS/ServiceConnect`)
   - `RequestCount`, `GrpcResponseStatusCode`, `HTTPCode_Target_2XX_Count`
5. Check if the port mapping in Service Connect config matches what the app actually listens on
6. Check if the client service is using the correct discovery name (not an IP or a wrong hostname)

**Common failures:**
| Symptom | Cause | Fix |
|---------|-------|-----|
| Connection refused to service name | Target service not registered or Envoy sidecar crashed | Check target service is running with Service Connect enabled |
| Timeout connecting to service | Network path issue (SGs between tasks don't allow the traffic) | Check both tasks' security groups allow the Service Connect port |
| 503 from Envoy | Target has no healthy upstreams | Target service tasks are failing — investigate the target service |
| Service starts then immediately fails | Envoy sidecar can't start (IAM issue or namespace not found) | Check execution role has Cloud Map permissions, namespace exists |

## Finding and Reading Task Logs

When you need application-level logs, the path is: task definition → logConfiguration → CloudWatch log group.

**Step 1: Get the log group from the task definition**

Describe the task definition (ecs:DescribeTaskDefinition). Each container has a `logConfiguration` block:
```json
"logConfiguration": {
  "logDriver": "awslogs",
  "options": {
    "awslogs-group": "/ecs/payment-service",
    "awslogs-region": "eu-central-1",
    "awslogs-stream-prefix": "ecs"
  }
}
```

The log stream name follows the pattern: `<prefix>/<container-name>/<task-id>`
Example: `ecs/payment-service/abc123def456`

**Step 2: Query the logs via CloudWatch MCP**

Once you have the log group, use the CloudWatch MCP tools:
- `analyze_log_group` with the log group name for automatic pattern/anomaly detection
- `execute_log_insights_query` for specific queries:
  ```
  fields @timestamp, @message
  | filter @logStream like /abc123/
  | sort @timestamp desc
  | limit 100
  ```
- To find errors across all tasks in the service:
  ```
  fields @timestamp, @message, @logStream
  | filter @message like /ERROR|Exception|FATAL/
  | sort @timestamp desc
  | limit 50
  ```

**Step 3: Correlate with task lifecycle**

If you know when a task stopped (from ecs:DescribeTasks `stoppedAt` timestamp), query logs just before that time to see what the container was doing when it died.

**Common log patterns to look for:**
- OOM: often no log at all (process killed mid-write) — last log entry is abruptly cut off
- Crash: stack trace or panic message in the last few lines before the stream ends
- Slow startup: health check timeout messages while the app is still initializing
- Connectivity: "connection refused", "timeout", "ECONNRESET" to downstream services

**If logDriver is not `awslogs`:**
- `splunk`, `fluentd`, `firelens` — logs go elsewhere, not queryable via CloudWatch MCP
- Say what log driver is configured and that you can't read those logs directly

## Metrics to Check

| Metric | Namespace | What it tells you |
|--------|-----------|-------------------|
| CPUUtilization | AWS/ECS | Service compute pressure (can indicate undersized tasks) |
| MemoryUtilization | AWS/ECS | Memory pressure (high = risk of OOM) |
| RunningTaskCount | AWS/ECS | How many tasks are actually running vs desired |
| DesiredTaskCount | AWS/ECS | What the service wants |
| HealthyHostCount | AWS/ApplicationELB | How many tasks are passing health checks |
| UnHealthyHostCount | AWS/ApplicationELB | How many are failing |
| TargetResponseTime | AWS/ApplicationELB | Latency from ALB to container |
| HTTPCode_Target_5XX_Count | AWS/ApplicationELB | Application errors |

## CloudTrail Events That Matter

| Event | Significance |
|-------|-------------|
| UpdateService | Deployment trigger (new task def, count change, config) |
| RegisterTaskDefinition | New task definition revision |
| CreateService / DeleteService | Service lifecycle |
| StopTask | Manual task termination |
| RunTask | One-off task execution |
| PutClusterCapacityProviders | Capacity provider changes |
| UpdateCluster / UpdateClusterSettings | Cluster-level config |

## What to Report

- Service state: running/desired/pending counts
- If tasks are failing: the specific stopCode, stoppedReason, and exit codes
- Whether a recent deployment caused the issue (with what changed in the task def)
- Load balancer target health (if applicable)
- Root cause classification (image issue / resource issue / config issue / IAM issue / networking)
- Recommended fix (specific: "increase memory to X", "fix image tag to Y", "add permission Z to execution role")
- What you couldn't verify

For a complete example of a well-structured investigation output, see [examples/investigation-output.md](examples/investigation-output.md).

## Tool Availability

This skill uses:
- `awslabs.cloudwatch-mcp-server` for metrics and logs
- `awslabs.aws-api-mcp-server` for ecs:Describe*, ecs:List*, elasticloadbalancing:DescribeTargetHealth, CloudTrail

If the AWS API MCP isn't connected, you can still check CloudWatch metrics (RunningTaskCount, CPUUtilization, MemoryUtilization, HealthyHostCount) and logs. Say what you would check with full API access.
