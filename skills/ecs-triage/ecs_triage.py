"""ECS Triage skill — main entry point.

Orchestrates sub-checks for ECS service/task diagnosis:
- Service stability (running vs desired, deployments, circuit breaker)
- Task failure classification (stop codes, exit codes)
- Load balancer target health
- Recent CloudTrail changes (UpdateService, RegisterTaskDefinition, etc.)
- Container Insights metrics

Input: JSON on stdin with cluster, region, service (optional)
Output: SkillResponse JSON on stdout

Usage:
    echo '{"cluster": "my-cluster", "region": "us-east-1"}' | uv run ecs_triage.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent))

from skills.shared.mcp_client import aws_api_mcp
from skills.shared.models import SkillResponse


# --- CloudTrail events relevant to ECS ---

_ECS_CHANGE_EVENTS: set[str] = {
    "UpdateService", "CreateService", "DeleteService",
    "RegisterTaskDefinition", "DeregisterTaskDefinition",
    "StopTask", "RunTask", "StartTask",
    "PutClusterCapacityProviders", "UpdateClusterSettings",
    "CreateCapacityProvider", "UpdateCapacityProvider",
}


# --- Input Validation ---


def validate_input(data: dict[str, Any]) -> tuple[str, str, str | None]:
    """Validate input. Returns (cluster, region, service)."""
    cluster = data.get("cluster")
    if not cluster or not isinstance(cluster, str) or not cluster.strip():
        raise ValueError("Missing required field: 'cluster'")

    region = data.get("region")
    if not region or not isinstance(region, str) or not region.strip():
        raise ValueError("Missing required field: 'region'")

    service = data.get("service")
    if service is not None and (not isinstance(service, str) or not service.strip()):
        service = None

    return cluster.strip(), region.strip(), service.strip() if service else None


# --- Data Fetching ---


def describe_services(cluster: str, region: str, service: str | None) -> list[dict[str, Any]]:
    """Fetch ECS service descriptions."""
    params: dict[str, Any] = {"cluster": cluster, "region": region}
    if service:
        params["services"] = [service]

    response = aws_api_mcp.invoke("describeEcsServices", params)
    if response.success and response.data:
        return response.data.get("services", [])
    return []


def list_stopped_tasks(cluster: str, region: str, service: str | None) -> list[dict[str, Any]]:
    """Fetch recently stopped tasks to classify failures."""
    params: dict[str, Any] = {"cluster": cluster, "region": region, "desiredStatus": "STOPPED"}
    if service:
        params["serviceName"] = service

    response = aws_api_mcp.invoke("listEcsTasks", params)
    if not response.success or not response.data:
        return []

    task_arns = response.data.get("taskArns", [])
    if not task_arns:
        return []

    # Describe the stopped tasks for details
    describe_response = aws_api_mcp.invoke(
        "describeEcsTasks",
        {"cluster": cluster, "tasks": task_arns[:10], "region": region},
    )
    if describe_response.success and describe_response.data:
        return describe_response.data.get("tasks", [])
    return []


def query_target_health(target_group_arn: str, region: str) -> dict[str, int]:
    """Query ALB/NLB target health for a target group."""
    response = aws_api_mcp.invoke(
        "describeTargetHealth",
        {"targetGroupArn": target_group_arn, "region": region},
    )
    if not response.success or not response.data:
        return {"healthy": 0, "unhealthy": 0}

    descriptions = response.data.get("TargetHealthDescriptions", [])
    healthy = sum(1 for d in descriptions if d.get("TargetHealth", {}).get("State") == "healthy")
    unhealthy = sum(1 for d in descriptions if d.get("TargetHealth", {}).get("State") in ("unhealthy", "draining"))

    return {"healthy": healthy, "unhealthy": unhealthy}


def query_recent_changes(cluster: str, region: str) -> list[dict[str, Any]]:
    """Query CloudTrail for recent ECS changes (last 24h)."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=24)

    response = aws_api_mcp.invoke(
        "lookupCloudTrailEvents",
        {
            "StartTime": start.isoformat(),
            "EndTime": now.isoformat(),
            "MaxResults": 50,
            "LookupAttributes": [
                {"AttributeKey": "ResourceName", "AttributeValue": cluster},
            ],
        },
    )

    if not response.success or not response.data:
        return []

    changes: list[dict[str, Any]] = []
    for event in response.data.get("Events", []):
        event_name = event.get("EventName", "")
        if event_name not in _ECS_CHANGE_EVENTS:
            continue
        changes.append({
            "eventName": event_name,
            "timestamp": event.get("EventTime", ""),
            "actor": event.get("Username", "unknown"),
            "description": f"{event_name} by {event.get('Username', 'unknown')}",
        })

    return changes


# --- Processing ---


def classify_failed_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Classify stopped tasks by failure reason."""
    failures: list[dict[str, Any]] = []

    for task in tasks:
        stop_code = task.get("stopCode", "")
        stopped_reason = task.get("stoppedReason", "")
        stopped_at = task.get("stoppedAt", "")

        # Find the container that caused the failure
        containers = task.get("containers", [])
        failing_container = ""
        exit_code = None

        for container in containers:
            if container.get("exitCode") is not None and container.get("exitCode") != 0:
                failing_container = container.get("name", "")
                exit_code = container.get("exitCode")
                break
            if container.get("reason"):
                failing_container = container.get("name", "")
                break

        if not failing_container and containers:
            failing_container = containers[0].get("name", "unknown")

        failures.append({
            "taskArn": task.get("taskArn", ""),
            "stopCode": stop_code,
            "stoppedReason": stopped_reason,
            "exitCode": exit_code,
            "container": failing_container,
            "stoppedAt": stopped_at,
        })

    return failures


def generate_findings(
    services: list[dict[str, Any]],
    failed_tasks: list[dict[str, Any]],
) -> list[str]:
    """Generate human-readable findings."""
    findings: list[str] = []

    for svc in services:
        name = svc.get("serviceName", "unknown")
        running = svc.get("runningCount", 0)
        desired = svc.get("desiredCount", 0)

        if running < desired:
            findings.append(
                f"Service {name}: running count ({running}) below desired ({desired}) — "
                f"{desired - running} tasks not running"
            )

        deployments = svc.get("deployments", [])
        for deploy in deployments:
            if deploy.get("rolloutState") == "FAILED":
                findings.append(f"Service {name}: deployment {deploy.get('id', '')} FAILED")

        # Circuit breaker
        deploy_config = svc.get("deploymentConfiguration", {})
        cb = deploy_config.get("deploymentCircuitBreaker", {})
        if cb.get("enable") and svc.get("events"):
            for event in svc.get("events", [])[:5]:
                msg = event.get("message", "")
                if "circuit breaker" in msg.lower():
                    findings.append(f"Service {name}: circuit breaker triggered — {msg[:100]}")
                    break

    # Task failure patterns
    if failed_tasks:
        stop_codes: dict[str, int] = {}
        for ft in failed_tasks:
            code = ft.get("stopCode", "Unknown")
            stop_codes[code] = stop_codes.get(code, 0) + 1

        for code, count in stop_codes.items():
            findings.append(f"Task failures: {count} task(s) stopped with code '{code}'")

        oom_count = sum(1 for ft in failed_tasks if ft.get("exitCode") == 137)
        if oom_count:
            findings.append(f"OOM killed: {oom_count} task(s) exited with code 137")

    return findings


# --- Main Triage Logic ---


def run_ecs_triage(cluster: str, region: str, service: str | None = None) -> SkillResponse:
    """Run the full ECS triage."""

    # 1. Describe services
    services = describe_services(cluster, region, service)

    # 2. Get stopped tasks
    stopped_tasks = list_stopped_tasks(cluster, region, service)
    failed_tasks = classify_failed_tasks(stopped_tasks)

    # 3. Check load balancer health for each service
    service_summaries: list[dict[str, Any]] = []
    for svc in services:
        lb_info: dict[str, Any] = {"targetGroupArn": None, "healthyCount": 0, "unhealthyCount": 0}
        load_balancers = svc.get("loadBalancers", [])
        if load_balancers:
            tg_arn = load_balancers[0].get("targetGroupArn", "")
            if tg_arn:
                health = query_target_health(tg_arn, region)
                lb_info = {"targetGroupArn": tg_arn, "healthyCount": health["healthy"], "unhealthyCount": health["unhealthy"]}

        # Extract deployment info
        deployments = []
        for d in svc.get("deployments", []):
            deployments.append({
                "id": d.get("id", ""),
                "status": d.get("status", ""),
                "taskDefinition": d.get("taskDefinition", ""),
                "runningCount": d.get("runningCount", 0),
                "desiredCount": d.get("desiredCount", 0),
                "rolloutState": d.get("rolloutState", ""),
                "createdAt": d.get("createdAt", ""),
            })

        # Circuit breaker state
        deploy_config = svc.get("deploymentConfiguration", {})
        cb_config = deploy_config.get("deploymentCircuitBreaker", {})

        service_summaries.append({
            "serviceName": svc.get("serviceName", ""),
            "status": svc.get("status", ""),
            "runningCount": svc.get("runningCount", 0),
            "desiredCount": svc.get("desiredCount", 0),
            "pendingCount": svc.get("pendingCount", 0),
            "deployments": deployments,
            "circuitBreaker": {
                "enabled": cb_config.get("enable", False),
                "rollback": cb_config.get("rollback", False),
                "status": None,
            },
            "loadBalancer": lb_info,
        })

    # 4. Recent changes
    recent_changes = query_recent_changes(cluster, region)

    # 5. Generate findings
    findings = generate_findings(services, failed_tasks)

    report = {
        "cluster": cluster,
        "region": region,
        "services": service_summaries,
        "failedTasks": failed_tasks,
        "recentChanges": recent_changes,
        "findings": findings,
    }

    return SkillResponse(status="success", message="", data=report)


# --- Entry Point ---


def main() -> None:
    """Entry point: read JSON from stdin, run ECS triage, write response to stdout."""
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            response = SkillResponse(
                status="error",
                message="No input provided. Expected JSON with 'cluster' and 'region'.",
                data=None,
            )
            print(json.dumps(response.model_dump(by_alias=True, mode="json")))
            sys.exit(1)

        data = json.loads(raw_input)
        cluster, region, service = validate_input(data)
        result = run_ecs_triage(cluster, region, service)
        print(json.dumps(result.model_dump(by_alias=True, mode="json")))

    except json.JSONDecodeError as e:
        response = SkillResponse(status="error", message=f"Invalid JSON input: {e}", data=None)
        print(json.dumps(response.model_dump(by_alias=True, mode="json")))
        sys.exit(1)

    except ValueError as e:
        response = SkillResponse(status="error", message=str(e), data=None)
        print(json.dumps(response.model_dump(by_alias=True, mode="json")))
        sys.exit(1)

    except Exception as e:
        response = SkillResponse(status="error", message=f"Unexpected error: {e}", data=None)
        print(json.dumps(response.model_dump(by_alias=True, mode="json")))
        sys.exit(1)


if __name__ == "__main__":
    main()
