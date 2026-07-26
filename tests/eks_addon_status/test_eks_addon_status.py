"""Unit tests for the EKS Addon Status skill."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure the project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from skills.shared.mcp_client import MCPResponse
from skills.shared.models import AddonHealth, AddonStatus

# Import the skill functions directly from the hyphenated directory
sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent.parent / "skills" / "eks-addon-status"),
)
from eks_addon_status import (
    CHECKED_ADDONS,
    DEGRADED_STATUSES,
    build_finding_message,
    check_addon_health,
    parse_addon_health,
    validate_input,
    _map_addon_status,
)


class TestValidateInput:
    """Tests for input validation."""

    def test_valid_input(self):
        cluster, region = validate_input({"cluster": "prod-cluster", "region": "us-east-1"})
        assert cluster == "prod-cluster"
        assert region == "us-east-1"

    def test_strips_whitespace(self):
        cluster, region = validate_input({"cluster": "  prod  ", "region": "  us-west-2  "})
        assert cluster == "prod"
        assert region == "us-west-2"

    def test_missing_cluster(self):
        with pytest.raises(ValueError, match="cluster"):
            validate_input({"region": "us-east-1"})

    def test_empty_cluster(self):
        with pytest.raises(ValueError, match="cluster"):
            validate_input({"cluster": "", "region": "us-east-1"})

    def test_missing_region(self):
        with pytest.raises(ValueError, match="region"):
            validate_input({"cluster": "prod-cluster"})

    def test_empty_region(self):
        with pytest.raises(ValueError, match="region"):
            validate_input({"cluster": "prod-cluster", "region": "   "})


class TestMapAddonStatus:
    """Tests for status mapping."""

    def test_active_maps_correctly(self):
        assert _map_addon_status("active") == AddonStatus.ACTIVE

    def test_degraded_maps_correctly(self):
        assert _map_addon_status("degraded") == AddonStatus.DEGRADED

    def test_failed_maps_correctly(self):
        assert _map_addon_status("failed") == AddonStatus.FAILED

    def test_updating_maps_correctly(self):
        assert _map_addon_status("updating") == AddonStatus.UPDATING

    def test_creating_maps_to_updating(self):
        assert _map_addon_status("creating") == AddonStatus.UPDATING

    def test_deleting_maps_to_failed(self):
        assert _map_addon_status("deleting") == AddonStatus.FAILED

    def test_unknown_defaults_to_degraded(self):
        assert _map_addon_status("unknown-status") == AddonStatus.DEGRADED


class TestParseAddonHealth:
    """Tests for parsing addon health data."""

    def test_none_response_returns_degraded(self):
        result = parse_addon_health("vpc-cni", None)
        assert result.name == "vpc-cni"
        assert result.status == AddonStatus.DEGRADED
        assert result.version == "unknown"
        assert result.issues is not None
        assert len(result.issues) > 0

    def test_active_addon_parsed_correctly(self):
        raw = {
            "status": "ACTIVE",
            "addonVersion": "v1.14.1-eksbuild.1",
            "health": {"issues": []},
        }
        result = parse_addon_health("coredns", raw)
        assert result.name == "coredns"
        assert result.status == AddonStatus.ACTIVE
        assert result.version == "v1.14.1-eksbuild.1"
        assert result.issues is None

    def test_degraded_addon_with_issues(self):
        raw = {
            "status": "DEGRADED",
            "addonVersion": "v1.12.0",
            "health": {
                "issues": [
                    {"code": "InsufficientNumberOfReplicas", "message": "Not enough replicas running"}
                ]
            },
        }
        result = parse_addon_health("coredns", raw)
        assert result.name == "coredns"
        assert result.status == AddonStatus.DEGRADED
        assert result.version == "v1.12.0"
        assert result.issues is not None
        assert len(result.issues) == 1
        assert "InsufficientNumberOfReplicas" in result.issues[0]

    def test_failed_addon(self):
        raw = {
            "status": "FAILED",
            "addonVersion": "v1.11.0",
            "health": {
                "issues": [
                    {"code": "ConfigurationConflict", "message": "Addon configuration conflicts"}
                ]
            },
        }
        result = parse_addon_health("kube-proxy", raw)
        assert result.status == AddonStatus.FAILED

    def test_version_fallback(self):
        raw = {"status": "ACTIVE", "version": "fallback-version"}
        result = parse_addon_health("vpc-cni", raw)
        assert result.version == "fallback-version"

    def test_health_as_string(self):
        raw = {"status": "ACTIVE", "addonVersion": "v1.0", "health": "OK"}
        result = parse_addon_health("vpc-cni", raw)
        assert result.health == "OK"


class TestBuildFindingMessage:
    """Tests for finding message generation."""

    def test_single_degraded_addon(self):
        addons = [
            AddonHealth(name="vpc-cni", version="v1.0", status=AddonStatus.DEGRADED, issues=["IP exhaustion"])
        ]
        msg = build_finding_message(addons)
        assert "1 addon" in msg
        assert "vpc-cni" in msg
        assert "degraded" in msg
        assert "IP exhaustion" in msg

    def test_multiple_degraded_addons(self):
        addons = [
            AddonHealth(name="vpc-cni", version="v1.0", status=AddonStatus.DEGRADED),
            AddonHealth(name="coredns", version="v1.0", status=AddonStatus.FAILED, issues=["Crash"]),
        ]
        msg = build_finding_message(addons)
        assert "2 addons" in msg
        assert "vpc-cni" in msg
        assert "coredns" in msg

    def test_message_includes_status(self):
        addons = [
            AddonHealth(name="kube-proxy", version="v1.0", status=AddonStatus.FAILED)
        ]
        msg = build_finding_message(addons)
        assert "failed" in msg


class TestCheckAddonHealth:
    """Tests for the main check_addon_health function."""

    @patch("eks_addon_status.fetch_addon_status")
    def test_all_healthy(self, mock_fetch):
        """All addons active → overallHealthy is True, no finding."""
        mock_fetch.return_value = {
            "status": "ACTIVE",
            "addonVersion": "v1.0.0",
            "health": {"issues": []},
        }
        result = check_addon_health("my-cluster", "us-east-1")
        assert result.status == "success"
        assert result.data is not None
        assert result.data["overallHealthy"] is True
        assert result.data["degradedCount"] == 0
        assert result.data["finding"] is None
        assert len(result.data["addons"]) == 4

    @patch("eks_addon_status.fetch_addon_status")
    def test_one_degraded_triggers_finding(self, mock_fetch):
        """One degraded addon → overallHealthy is False, finding is set."""

        def side_effect(cluster, region, addon_name):
            if addon_name == "coredns":
                return {
                    "status": "DEGRADED",
                    "addonVersion": "v1.10.0",
                    "health": {"issues": [{"code": "Unhealthy", "message": "Pod not ready"}]},
                }
            return {
                "status": "ACTIVE",
                "addonVersion": "v1.0.0",
                "health": {"issues": []},
            }

        mock_fetch.side_effect = side_effect
        result = check_addon_health("my-cluster", "us-east-1")
        assert result.status == "success"
        assert result.data["overallHealthy"] is False
        assert result.data["degradedCount"] == 1
        assert result.data["finding"] is not None
        assert "coredns" in result.data["finding"]

    @patch("eks_addon_status.fetch_addon_status")
    def test_fetch_failure_marks_degraded(self, mock_fetch):
        """If fetch returns None for an addon, it's treated as degraded."""
        mock_fetch.return_value = None
        result = check_addon_health("my-cluster", "us-east-1")
        assert result.status == "success"
        assert result.data["overallHealthy"] is False
        assert result.data["degradedCount"] == 4  # All four addons fail

    @patch("eks_addon_status.fetch_addon_status")
    def test_updating_addon_not_degraded(self, mock_fetch):
        """Updating addons are not counted as degraded."""

        def side_effect(cluster, region, addon_name):
            if addon_name == "vpc-cni":
                return {"status": "UPDATING", "addonVersion": "v1.14.0", "health": {}}
            return {"status": "ACTIVE", "addonVersion": "v1.0.0", "health": {"issues": []}}

        mock_fetch.side_effect = side_effect
        result = check_addon_health("my-cluster", "us-east-1")
        assert result.data["overallHealthy"] is True
        assert result.data["degradedCount"] == 0

    @patch("eks_addon_status.fetch_addon_status")
    def test_response_includes_cluster_and_region(self, mock_fetch):
        """Output includes the cluster and region."""
        mock_fetch.return_value = {"status": "ACTIVE", "addonVersion": "v1.0", "health": {}}
        result = check_addon_health("prod-east", "us-east-1")
        assert result.data["cluster"] == "prod-east"
        assert result.data["region"] == "us-east-1"


class TestCheckedAddons:
    """Verify the four required addons are configured."""

    def test_vpc_cni_in_list(self):
        assert "vpc-cni" in CHECKED_ADDONS

    def test_coredns_in_list(self):
        assert "coredns" in CHECKED_ADDONS

    def test_kube_proxy_in_list(self):
        assert "kube-proxy" in CHECKED_ADDONS

    def test_ebs_csi_in_list(self):
        assert "aws-ebs-csi-driver" in CHECKED_ADDONS

    def test_exactly_four_addons(self):
        assert len(CHECKED_ADDONS) == 4
