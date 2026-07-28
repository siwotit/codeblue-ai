"""EKS Addon Status sub-module for EKS Triage.

Checks the health of critical EKS-managed addons:
- vpc-cni: Pod networking (ENI/IP assignment)
- coredns: Cluster DNS resolution
- kube-proxy: Service load balancing
- aws-ebs-csi-driver: EBS persistent volume provisioning
"""

from __future__ import annotations

from typing import Any

from skills.shared.mcp_client import aws_api_mcp
from skills.shared.models import AddonHealth, AddonStatus


CHECKED_ADDONS = ["vpc-cni", "coredns", "kube-proxy", "aws-ebs-csi-driver"]
DEGRADED_STATUSES = {AddonStatus.DEGRADED, AddonStatus.FAILED}


def fetch_addon_status(cluster: str, region: str, addon_name: str) -> dict[str, Any] | None:
    """Fetch addon status from aws-api-mcp."""
    response = aws_api_mcp.invoke(
        "describe_addon",
        {"cluster_name": cluster, "addon_name": addon_name, "region": region},
    )
    if response.success and response.data:
        return response.data.get("addon", response.data)
    return None


def parse_addon_health(addon_name: str, raw_addon: dict[str, Any] | None) -> AddonHealth:
    """Parse raw addon response into an AddonHealth model."""
    if raw_addon is None:
        return AddonHealth(
            name=addon_name, version="unknown", status=AddonStatus.DEGRADED,
            health="Unable to retrieve addon status",
            issues=["Failed to fetch addon status from aws-api-mcp"],
        )

    raw_status = raw_addon.get("status", "").lower()
    status = _map_addon_status(raw_status)
    version = raw_addon.get("addonVersion", raw_addon.get("version", "unknown"))

    health_info = raw_addon.get("health", {})
    health_summary: str | None = None
    issues: list[str] = []

    if isinstance(health_info, dict):
        health_issues = health_info.get("issues", [])
        for issue in health_issues:
            if isinstance(issue, dict):
                code = issue.get("code", "")
                msg = issue.get("message", "")
                issues.append(f"{code}: {msg}" if code else msg)
            elif isinstance(issue, str):
                issues.append(issue)
        if issues:
            health_summary = f"{len(issues)} issue(s) detected"
    elif isinstance(health_info, str):
        health_summary = health_info

    return AddonHealth(
        name=addon_name, version=version, status=status,
        health=health_summary, issues=issues if issues else None,
    )


def _map_addon_status(raw_status: str) -> AddonStatus:
    status_map = {
        "active": AddonStatus.ACTIVE,
        "degraded": AddonStatus.DEGRADED,
        "failed": AddonStatus.FAILED,
        "updating": AddonStatus.UPDATING,
        "creating": AddonStatus.UPDATING,
        "deleting": AddonStatus.FAILED,
    }
    return status_map.get(raw_status, AddonStatus.DEGRADED)


def check_addon_health(cluster: str, region: str) -> dict[str, Any]:
    """Check the health of all critical EKS addons."""
    addons: list[AddonHealth] = []

    for addon_name in CHECKED_ADDONS:
        raw_addon = fetch_addon_status(cluster, region, addon_name)
        addon_health = parse_addon_health(addon_name, raw_addon)
        addons.append(addon_health)

    degraded_addons = [a for a in addons if a.status in DEGRADED_STATUSES]
    overall_healthy = len(degraded_addons) == 0

    finding: str | None = None
    if degraded_addons:
        parts = [f"{a.name} ({a.status.value})" for a in degraded_addons]
        finding = f"EKS addons unhealthy: {'; '.join(parts)}"

    return {
        "cluster": cluster,
        "region": region,
        "addons": [a.model_dump(by_alias=True, mode="json") for a in addons],
        "degradedCount": len(degraded_addons),
        "overallHealthy": overall_healthy,
        "finding": finding,
    }
