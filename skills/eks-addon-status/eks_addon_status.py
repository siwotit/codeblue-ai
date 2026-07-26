"""EKS Addon Status skill script for CodeBlue AI.

Checks the health of critical EKS-managed addons:
- vpc-cni: Pod networking (ENI/IP assignment)
- coredns: Cluster DNS resolution
- kube-proxy: Service load balancing
- aws-ebs-csi-driver: EBS persistent volume provisioning

Triggers a degraded finding when any addon is in degraded or failed state.

Classification: Mixed (balanced script + prompt guidance)
Requirements: 3.5, 14.2

Usage:
    echo '{"cluster": "my-cluster", "region": "us-east-1"}' | uv run eks_addon_status.py
"""

from __future__ import annotations

import json
import sys
from typing import Any

# Add parent paths so shared modules are importable
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent))

from skills.shared.mcp_client import aws_api_mcp
from skills.shared.models import (
    AddonHealth,
    AddonStatus,
    SkillResponse,
)


# --- Constants ---

# The four critical EKS-managed addons to check
CHECKED_ADDONS = [
    "vpc-cni",
    "coredns",
    "kube-proxy",
    "aws-ebs-csi-driver",
]

# Statuses that indicate a degraded cluster health finding
DEGRADED_STATUSES = {AddonStatus.DEGRADED, AddonStatus.FAILED}


# --- Input Validation ---


def validate_input(data: dict[str, Any]) -> tuple[str, str]:
    """Validate and extract input fields.

    Returns:
        Tuple of (cluster, region).

    Raises:
        ValueError: If required fields are missing.
    """
    cluster = data.get("cluster")
    if not cluster or not isinstance(cluster, str) or not cluster.strip():
        raise ValueError("Missing required field: 'cluster'")

    region = data.get("region")
    if not region or not isinstance(region, str) or not region.strip():
        raise ValueError("Missing required field: 'region'")

    return cluster.strip(), region.strip()


# --- Data Fetching ---


def fetch_addon_status(
    cluster: str, region: str, addon_name: str
) -> dict[str, Any] | None:
    """Fetch addon status from the aws-api-mcp using EKS describe-addon.

    Args:
        cluster: EKS cluster name.
        region: AWS region.
        addon_name: Name of the addon to describe.

    Returns:
        Addon data dict if successful, None if the call fails.
    """
    response = aws_api_mcp.invoke(
        "describe_addon",
        {
            "cluster_name": cluster,
            "addon_name": addon_name,
            "region": region,
        },
    )
    if response.success and response.data:
        return response.data.get("addon", response.data)
    return None


# --- Processing ---


def parse_addon_health(
    addon_name: str, raw_addon: dict[str, Any] | None
) -> AddonHealth:
    """Parse raw addon response into an AddonHealth model.

    If raw_addon is None (fetch failed), returns a degraded addon entry
    indicating the data was unavailable.
    """
    if raw_addon is None:
        return AddonHealth(
            name=addon_name,
            version="unknown",
            status=AddonStatus.DEGRADED,
            health="Unable to retrieve addon status",
            issues=["Failed to fetch addon status from aws-api-mcp"],
        )

    # Extract status from the raw addon data
    raw_status = raw_addon.get("status", "").lower()
    status = _map_addon_status(raw_status)

    # Extract version
    version = raw_addon.get("addonVersion", raw_addon.get("version", "unknown"))

    # Extract health information
    health_info = raw_addon.get("health", {})
    health_summary: str | None = None
    issues: list[str] = []

    if isinstance(health_info, dict):
        health_issues = health_info.get("issues", [])
        if health_issues:
            for issue in health_issues:
                if isinstance(issue, dict):
                    code = issue.get("code", "")
                    msg = issue.get("message", "")
                    issues.append(f"{code}: {msg}" if code else msg)
                elif isinstance(issue, str):
                    issues.append(issue)
            health_summary = f"{len(issues)} issue(s) detected"
    elif isinstance(health_info, str):
        health_summary = health_info

    return AddonHealth(
        name=addon_name,
        version=version,
        status=status,
        health=health_summary,
        issues=issues if issues else None,
    )


def _map_addon_status(raw_status: str) -> AddonStatus:
    """Map a raw status string to the AddonStatus enum.

    Args:
        raw_status: Lowercase status string from EKS API.

    Returns:
        Corresponding AddonStatus enum value.
    """
    status_map = {
        "active": AddonStatus.ACTIVE,
        "degraded": AddonStatus.DEGRADED,
        "failed": AddonStatus.FAILED,
        "updating": AddonStatus.UPDATING,
        "creating": AddonStatus.UPDATING,
        "deleting": AddonStatus.FAILED,
    }
    return status_map.get(raw_status, AddonStatus.DEGRADED)


def build_finding_message(degraded_addons: list[AddonHealth]) -> str:
    """Build a human-readable degraded finding message.

    Args:
        degraded_addons: List of addons in degraded or failed state.

    Returns:
        Finding message string.
    """
    parts: list[str] = []
    for addon in degraded_addons:
        detail = f"{addon.name} ({addon.status.value})"
        if addon.issues:
            detail += f" — {addon.issues[0]}"
        parts.append(detail)

    addon_list = "; ".join(parts)
    count = len(degraded_addons)
    noun = "addon" if count == 1 else "addons"
    return (
        f"EKS cluster health degraded: {count} {noun} in unhealthy state: {addon_list}"
    )


# --- Main Logic ---


def check_addon_health(cluster: str, region: str) -> SkillResponse:
    """Check the health of all critical EKS addons.

    Fetches each addon's status via aws-api-mcp (EKS describe-addon),
    parses the results, and triggers a degraded finding when any addon
    is in degraded or failed state.

    Args:
        cluster: EKS cluster name.
        region: AWS region.

    Returns:
        SkillResponse with addon health data.
    """
    addons: list[AddonHealth] = []

    for addon_name in CHECKED_ADDONS:
        raw_addon = fetch_addon_status(cluster, region, addon_name)
        addon_health = parse_addon_health(addon_name, raw_addon)
        addons.append(addon_health)

    # Identify degraded/failed addons
    degraded_addons = [a for a in addons if a.status in DEGRADED_STATUSES]
    degraded_count = len(degraded_addons)
    overall_healthy = degraded_count == 0

    # Build finding message if any addon is unhealthy
    finding: str | None = None
    if degraded_addons:
        finding = build_finding_message(degraded_addons)

    # Serialize addon data
    addon_dicts = [a.model_dump(by_alias=True, mode="json") for a in addons]

    return SkillResponse(
        status="success",
        message="",
        data={
            "cluster": cluster,
            "region": region,
            "addons": addon_dicts,
            "degradedCount": degraded_count,
            "overallHealthy": overall_healthy,
            "finding": finding,
        },
    )


def main() -> None:
    """Entry point: read JSON from stdin, check addon health, write response to stdout."""
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            response = SkillResponse(
                status="error",
                message="No input provided. Expected JSON with 'cluster' and 'region' fields.",
                data=None,
            )
            print(json.dumps(response.model_dump(by_alias=True, mode="json")))
            sys.exit(1)

        data = json.loads(raw_input)
        cluster, region = validate_input(data)
        result = check_addon_health(cluster, region)
        print(json.dumps(result.model_dump(by_alias=True, mode="json")))

    except json.JSONDecodeError as e:
        response = SkillResponse(
            status="error",
            message=f"Invalid JSON input: {e}",
            data=None,
        )
        print(json.dumps(response.model_dump(by_alias=True, mode="json")))
        sys.exit(1)

    except ValueError as e:
        response = SkillResponse(
            status="error",
            message=str(e),
            data=None,
        )
        print(json.dumps(response.model_dump(by_alias=True, mode="json")))
        sys.exit(1)

    except Exception as e:
        response = SkillResponse(
            status="error",
            message=f"Unexpected error during EKS addon health check: {e}",
            data=None,
        )
        print(json.dumps(response.model_dump(by_alias=True, mode="json")))
        sys.exit(1)


if __name__ == "__main__":
    main()
