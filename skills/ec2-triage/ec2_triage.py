"""EC2 Triage skill — main entry point.

Orchestrates sub-checks for EC2 instance diagnosis:
- Instance status checks (system + instance)
- Health metrics (CPU, network, disk, status check failures)
- Security group and networking state
- Recent CloudTrail changes (stop/start/modify/terminate, SG changes)
- Auto Scaling group membership

Input: JSON on stdin with instanceIds, region
Output: SkillResponse JSON on stdout

Usage:
    echo '{"instanceIds": ["i-abc123"], "region": "us-east-1"}' | uv run ec2_triage.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent))

from skills.shared.mcp_client import aws_api_mcp, cloudwatch_mcp
from skills.shared.models import SkillResponse


# --- CloudTrail write events relevant to EC2 ---

_EC2_CHANGE_EVENTS: set[str] = {
    "StopInstances", "StartInstances", "RebootInstances", "TerminateInstances",
    "RunInstances", "ModifyInstanceAttribute", "AttachVolume", "DetachVolume",
    "AuthorizeSecurityGroupIngress", "AuthorizeSecurityGroupEgress",
    "RevokeSecurityGroupIngress", "RevokeSecurityGroupEgress",
    "ModifyNetworkInterfaceAttribute", "AssociateAddress", "DisassociateAddress",
}


# --- Input Validation ---


def validate_input(data: dict[str, Any]) -> tuple[list[str], str]:
    """Validate input. Returns (instance_ids, region)."""
    instance_ids = data.get("instanceIds")
    if not instance_ids or not isinstance(instance_ids, list):
        raise ValueError("Missing required field: 'instanceIds' (must be a non-empty list)")

    region = data.get("region")
    if not region or not isinstance(region, str) or not region.strip():
        raise ValueError("Missing required field: 'region'")

    return [i.strip() for i in instance_ids if isinstance(i, str) and i.strip()], region.strip()


# --- Data Fetching ---


def describe_instances(instance_ids: list[str], region: str) -> list[dict[str, Any]]:
    """Fetch instance descriptions from aws-api-mcp."""
    response = aws_api_mcp.invoke(
        "describeInstances",
        {"instanceIds": instance_ids, "region": region},
    )
    if response.success and response.data:
        reservations = response.data.get("Reservations", [])
        instances = []
        for r in reservations:
            instances.extend(r.get("Instances", []))
        return instances
    return []


def describe_instance_status(instance_ids: list[str], region: str) -> dict[str, dict[str, str]]:
    """Fetch instance status checks from aws-api-mcp."""
    response = aws_api_mcp.invoke(
        "describeInstanceStatus",
        {"instanceIds": instance_ids, "region": region, "includeAllInstances": True},
    )
    if not response.success or not response.data:
        return {}

    statuses: dict[str, dict[str, str]] = {}
    for status in response.data.get("InstanceStatuses", []):
        iid = status.get("InstanceId", "")
        sys_status = status.get("SystemStatus", {}).get("Status", "unknown")
        inst_status = status.get("InstanceStatus", {}).get("Status", "unknown")
        statuses[iid] = {"system": sys_status, "instance": inst_status}

    return statuses


def fetch_instance_metrics(instance_id: str, region: str) -> dict[str, float | None]:
    """Fetch key metrics for an instance from CloudWatch."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=10)

    metrics_to_fetch = [
        ("CPUUtilization", "AWS/EC2", "Average"),
        ("NetworkIn", "AWS/EC2", "Sum"),
        ("NetworkOut", "AWS/EC2", "Sum"),
        ("StatusCheckFailed", "AWS/EC2", "Maximum"),
    ]

    result: dict[str, float | None] = {}
    for metric_name, namespace, stat in metrics_to_fetch:
        response = cloudwatch_mcp.invoke(
            "getMetricData",
            {
                "metricName": metric_name,
                "namespace": namespace,
                "dimensions": [{"Name": "InstanceId", "Value": instance_id}],
                "startTime": start.isoformat(),
                "endTime": now.isoformat(),
                "period": 300,
                "stat": stat,
            },
        )
        if response.success and response.data:
            data_points = response.data.get("dataPoints", [])
            if data_points:
                result[metric_name] = data_points[-1].get("value")
            else:
                result[metric_name] = None
        else:
            result[metric_name] = None

    return result


def query_recent_changes(instance_ids: list[str], region: str) -> list[dict[str, Any]]:
    """Query CloudTrail for recent EC2 changes (last 24h)."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=24)

    response = aws_api_mcp.invoke(
        "lookupCloudTrailEvents",
        {
            "StartTime": start.isoformat(),
            "EndTime": now.isoformat(),
            "MaxResults": 50,
            "LookupAttributes": [
                {"AttributeKey": "ResourceName", "AttributeValue": iid}
                for iid in instance_ids[:3]
            ],
        },
    )

    if not response.success or not response.data:
        return []

    changes: list[dict[str, Any]] = []
    for event in response.data.get("Events", []):
        event_name = event.get("EventName", "")
        if event_name not in _EC2_CHANGE_EVENTS:
            continue
        changes.append({
            "eventName": event_name,
            "timestamp": event.get("EventTime", ""),
            "actor": event.get("Username", "unknown"),
            "description": f"{event_name} by {event.get('Username', 'unknown')}",
        })

    return changes


# --- Finding Generation ---


def generate_findings(
    instances: list[dict[str, Any]],
    statuses: dict[str, dict[str, str]],
    metrics: dict[str, dict[str, float | None]],
) -> list[str]:
    """Generate human-readable findings from collected data."""
    findings: list[str] = []

    for inst in instances:
        iid = inst.get("InstanceId", "unknown")
        state = inst.get("State", {}).get("Name", "unknown")

        if state != "running":
            findings.append(f"Instance {iid} is in '{state}' state (not running)")

        status = statuses.get(iid, {})
        if status.get("system") == "impaired":
            findings.append(f"Instance {iid}: SYSTEM status check IMPAIRED — likely host hardware issue")
        if status.get("instance") == "impaired":
            findings.append(f"Instance {iid}: INSTANCE status check IMPAIRED — likely guest OS issue")

        inst_metrics = metrics.get(iid, {})
        cpu = inst_metrics.get("CPUUtilization")
        if cpu is not None and cpu > 95.0:
            findings.append(f"Instance {iid}: CPU utilization at {cpu:.1f}% — compute-bound")

        status_check_failed = inst_metrics.get("StatusCheckFailed")
        if status_check_failed is not None and status_check_failed > 0:
            findings.append(f"Instance {iid}: Status check failed (value={status_check_failed})")

        net_in = inst_metrics.get("NetworkIn")
        net_out = inst_metrics.get("NetworkOut")
        if net_in == 0 and net_out == 0:
            findings.append(f"Instance {iid}: Zero network traffic — possible connectivity loss")

    return findings


# --- Main Triage Logic ---


def run_ec2_triage(instance_ids: list[str], region: str) -> SkillResponse:
    """Run the full EC2 triage."""

    # 1. Describe instances
    instances = describe_instances(instance_ids, region)

    # 2. Status checks
    statuses = describe_instance_status(instance_ids, region)

    # 3. Metrics for each instance
    all_metrics: dict[str, dict[str, float | None]] = {}
    for iid in instance_ids:
        all_metrics[iid] = fetch_instance_metrics(iid, region)

    # 4. Recent changes
    recent_changes = query_recent_changes(instance_ids, region)

    # 5. Generate findings
    findings = generate_findings(instances, statuses, all_metrics)

    # Build instance summaries
    instance_summaries = []
    for inst in instances:
        iid = inst.get("InstanceId", "unknown")
        sgs = [{"id": sg.get("GroupId", ""), "name": sg.get("GroupName", "")} for sg in inst.get("SecurityGroups", [])]

        # Check for ASG tag
        asg_name = None
        for tag in inst.get("Tags", []):
            if tag.get("Key") == "aws:autoscaling:groupName":
                asg_name = tag.get("Value")

        instance_summaries.append({
            "instanceId": iid,
            "state": inst.get("State", {}).get("Name", "unknown"),
            "statusChecks": statuses.get(iid, {"system": "unknown", "instance": "unknown"}),
            "metrics": {
                "cpuUtilization": all_metrics.get(iid, {}).get("CPUUtilization"),
                "networkIn": all_metrics.get(iid, {}).get("NetworkIn"),
                "networkOut": all_metrics.get(iid, {}).get("NetworkOut"),
                "statusCheckFailed": all_metrics.get(iid, {}).get("StatusCheckFailed"),
            },
            "securityGroups": sgs,
            "subnetId": inst.get("SubnetId", ""),
            "vpcId": inst.get("VpcId", ""),
            "autoScalingGroup": asg_name,
            "launchTime": inst.get("LaunchTime", ""),
        })

    report = {
        "region": region,
        "instances": instance_summaries,
        "recentChanges": recent_changes,
        "findings": findings,
    }

    return SkillResponse(status="success", message="", data=report)


# --- Entry Point ---


def main() -> None:
    """Entry point: read JSON from stdin, run EC2 triage, write response to stdout."""
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            response = SkillResponse(
                status="error",
                message="No input provided. Expected JSON with 'instanceIds' and 'region'.",
                data=None,
            )
            print(json.dumps(response.model_dump(by_alias=True, mode="json")))
            sys.exit(1)

        data = json.loads(raw_input)
        instance_ids, region = validate_input(data)
        result = run_ec2_triage(instance_ids, region)
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
