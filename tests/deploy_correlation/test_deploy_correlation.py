"""Unit tests for the deploy_correlation skill.

Tests core logic: temporal proximity scoring, resource overlap detection,
correlation score computation, and the main run function.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "skills"))

from shared.mcp_client import MCPResponse

# Import the module under test
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "skills" / "deploy-correlation"))
import deploy_correlation as dc


# ---------------------------------------------------------------------------
# Temporal proximity tests (Requirement 4.3)
# ---------------------------------------------------------------------------


class TestTemporalProximity:
    """Tests for compute_temporal_proximity."""

    def test_change_at_incident_time_returns_1(self):
        """A change exactly at incident time has proximity 1.0."""
        incident = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        change = incident
        lookback_seconds = 86400.0  # 24h

        result = dc.compute_temporal_proximity(change, incident, lookback_seconds)
        assert result == 1.0

    def test_change_at_lookback_boundary_returns_0(self):
        """A change at the far edge of the lookback window has proximity 0.0."""
        incident = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        change = incident - timedelta(hours=24)
        lookback_seconds = 86400.0

        result = dc.compute_temporal_proximity(change, incident, lookback_seconds)
        assert result == 0.0

    def test_change_5_minutes_before_incident(self):
        """A change 5 minutes before incident has very high proximity."""
        incident = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        change = incident - timedelta(minutes=5)
        lookback_seconds = 86400.0

        result = dc.compute_temporal_proximity(change, incident, lookback_seconds)
        # 5 min = 300s / 86400 = ~0.0035 delta → proximity ~0.9965
        assert result > 0.99

    def test_change_12_hours_before_incident(self):
        """A change 12 hours before incident has proximity ~0.5."""
        incident = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        change = incident - timedelta(hours=12)
        lookback_seconds = 86400.0

        result = dc.compute_temporal_proximity(change, incident, lookback_seconds)
        assert result == 0.5

    def test_change_beyond_lookback_returns_0(self):
        """A change beyond the lookback window has proximity 0.0 (clamped)."""
        incident = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        change = incident - timedelta(hours=48)
        lookback_seconds = 86400.0

        result = dc.compute_temporal_proximity(change, incident, lookback_seconds)
        assert result == 0.0

    def test_zero_lookback_returns_0(self):
        """Zero lookback seconds returns 0.0 without division error."""
        incident = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        change = incident - timedelta(minutes=5)

        result = dc.compute_temporal_proximity(change, incident, 0.0)
        assert result == 0.0


# ---------------------------------------------------------------------------
# Resource overlap tests (Requirement 4.4)
# ---------------------------------------------------------------------------


class TestResourceOverlap:
    """Tests for check_resource_overlap."""

    def test_exact_arn_match(self):
        """Exact ARN match is detected as overlap."""
        change_resource = "arn:aws:ecs:us-east-1:123456789012:service/prod/payments"
        affected = [
            {"type": "arn", "value": "arn:aws:ecs:us-east-1:123456789012:service/prod/payments", "displayName": "payments"}
        ]
        assert dc.check_resource_overlap(change_resource, affected) is True

    def test_substring_match(self):
        """Substring match is detected (resource value contained in change)."""
        change_resource = "arn:aws:ecs:us-east-1:123456789012:service/prod/payments"
        affected = [
            {"type": "k8s_resource", "value": "prod/payments", "displayName": "payments"}
        ]
        assert dc.check_resource_overlap(change_resource, affected) is True

    def test_no_match(self):
        """Non-matching resources return False."""
        change_resource = "arn:aws:ecs:us-east-1:123456789012:service/prod/orders"
        affected = [
            {"type": "arn", "value": "arn:aws:ecs:us-east-1:123456789012:service/prod/payments", "displayName": "payments"}
        ]
        assert dc.check_resource_overlap(change_resource, affected) is False

    def test_empty_resources_returns_false(self):
        """Empty affected resources list returns False."""
        assert dc.check_resource_overlap("some-resource", []) is False

    def test_empty_change_resource_returns_false(self):
        """Empty change resource returns False."""
        affected = [{"type": "arn", "value": "some-arn", "displayName": "x"}]
        assert dc.check_resource_overlap("", affected) is False

    def test_case_insensitive_match(self):
        """Matching is case-insensitive."""
        change_resource = "ARN:AWS:ECS:US-EAST-1:123:service/Prod/Payments"
        affected = [
            {"type": "arn", "value": "arn:aws:ecs:us-east-1:123:service/prod/payments", "displayName": "payments"}
        ]
        assert dc.check_resource_overlap(change_resource, affected) is True


# ---------------------------------------------------------------------------
# Correlation score tests (Requirements 4.5, 4.6)
# ---------------------------------------------------------------------------


class TestCorrelationScore:
    """Tests for compute_correlation_score."""

    def test_score_between_0_and_1(self):
        """Correlation score is always between 0.0 and 1.0."""
        for proximity in [0.0, 0.3, 0.5, 0.7, 0.9, 1.0]:
            for overlap in [True, False]:
                score = dc.compute_correlation_score(proximity, overlap)
                assert 0.0 <= score <= 1.0, f"Score {score} for proximity={proximity}, overlap={overlap}"

    def test_both_factors_higher_than_only_proximity(self):
        """Requirement 4.6: Both factors produce higher score than proximity alone."""
        proximity = 0.8
        score_both = dc.compute_correlation_score(proximity, True)
        score_proximity_only = dc.compute_correlation_score(proximity, False)
        assert score_both > score_proximity_only

    def test_both_factors_higher_than_only_overlap(self):
        """Both factors produce higher score than overlap alone (low proximity)."""
        high_proximity_both = dc.compute_correlation_score(0.9, True)
        low_proximity_overlap = dc.compute_correlation_score(0.1, True)
        assert high_proximity_both > low_proximity_overlap

    def test_zero_proximity_no_overlap_returns_zero(self):
        """Zero proximity and no overlap produces score 0.0."""
        score = dc.compute_correlation_score(0.0, False)
        assert score == 0.0

    def test_monotonicity_with_overlap(self):
        """Higher proximity with overlap always yields >= score than lower proximity with overlap."""
        for low_prox in [0.1, 0.3, 0.5]:
            for high_prox in [low_prox + 0.1, low_prox + 0.3, min(low_prox + 0.5, 1.0)]:
                score_low = dc.compute_correlation_score(low_prox, True)
                score_high = dc.compute_correlation_score(high_prox, True)
                assert score_high >= score_low, (
                    f"Monotonicity violated: score({high_prox}, True)={score_high} < score({low_prox}, True)={score_low}"
                )


# ---------------------------------------------------------------------------
# Lookback window parsing
# ---------------------------------------------------------------------------


class TestParseLookbackWindow:
    """Tests for parse_lookback_window."""

    def test_24h(self):
        result = dc.parse_lookback_window("24h")
        assert result == timedelta(hours=24)

    def test_48h(self):
        result = dc.parse_lookback_window("48h")
        assert result == timedelta(hours=48)

    def test_invalid_defaults_to_24h(self):
        result = dc.parse_lookback_window("invalid")
        assert result == timedelta(hours=24)


# ---------------------------------------------------------------------------
# Change type classification
# ---------------------------------------------------------------------------


class TestClassification:
    """Tests for event classification helpers."""

    def test_cloudtrail_deployment_event(self):
        assert dc.classify_cloudtrail_event("UpdateService") == dc.ChangeType.DEPLOYMENT

    def test_cloudtrail_scaling_event(self):
        assert dc.classify_cloudtrail_event("PutScalingPolicy") == dc.ChangeType.SCALING_EVENT

    def test_cloudtrail_iam_event(self):
        assert dc.classify_cloudtrail_event("UpdateRolePolicy") == dc.ChangeType.IAM_CHANGE

    def test_cloudtrail_config_change(self):
        assert dc.classify_cloudtrail_event("PutParameter") == dc.ChangeType.CONFIG_CHANGE

    def test_k8s_rollout_event(self):
        assert dc.classify_k8s_event("ScalingReplicaSet", "Deployment") == dc.ChangeType.ROLLOUT

    def test_k8s_scaling_event(self):
        assert dc.classify_k8s_event("SuccessfulRescale", "HorizontalPodAutoscaler") == dc.ChangeType.SCALING_EVENT


# ---------------------------------------------------------------------------
# Integration tests for run_deploy_correlation
# ---------------------------------------------------------------------------


class TestRunDeployCorrelation:
    """Tests for the main run_deploy_correlation function."""

    def test_missing_affected_resources(self):
        """Missing affectedResources returns error."""
        result = dc.run_deploy_correlation({"incidentTime": "2024-01-15T10:00:00Z"})
        assert result["status"] == "error"
        assert "affectedResources" in result["message"]

    def test_missing_incident_time(self):
        """Missing incidentTime returns error."""
        result = dc.run_deploy_correlation({
            "affectedResources": [{"type": "arn", "value": "arn:aws:x", "displayName": "x"}],
        })
        assert result["status"] == "error"
        assert "incidentTime" in result["message"]

    def test_invalid_incident_time(self):
        """Invalid incidentTime format returns error."""
        result = dc.run_deploy_correlation({
            "affectedResources": [{"type": "arn", "value": "arn:aws:x", "displayName": "x"}],
            "incidentTime": "not-a-date",
        })
        assert result["status"] == "error"
        assert "incidentTime" in result["message"]

    @patch("deploy_correlation.query_cloudtrail_changes")
    @patch("deploy_correlation.query_kubernetes_changes")
    def test_no_changes_found(self, mock_k8s, mock_ct):
        """No changes found returns empty list with metadata (Req 4.7)."""
        mock_ct.return_value = ([], None)
        mock_k8s.return_value = ([], None)

        result = dc.run_deploy_correlation({
            "affectedResources": [
                {"type": "arn", "value": "arn:aws:ecs:us-east-1:123:service/prod/payments", "displayName": "payments"}
            ],
            "incidentTime": "2024-01-15T10:00:00Z",
            "lookbackWindow": "24h",
        })

        assert result["status"] == "success"
        assert result["data"]["changes"] == []
        assert result["data"]["lookbackWindow"] == "24h"
        assert len(result["data"]["affectedResources"]) == 1

    @patch("deploy_correlation.query_cloudtrail_changes")
    @patch("deploy_correlation.query_kubernetes_changes")
    def test_both_sources_fail_returns_error(self, mock_k8s, mock_ct):
        """Both sources failing returns error response (Req 4.8)."""
        mock_ct.return_value = ([], "access denied")
        mock_k8s.return_value = ([], "connection timeout")

        result = dc.run_deploy_correlation({
            "affectedResources": [
                {"type": "arn", "value": "arn:aws:ecs:us-east-1:123:service/prod/payments", "displayName": "payments"}
            ],
            "incidentTime": "2024-01-15T10:00:00Z",
        })

        assert result["status"] == "error"
        assert "unavailable" in result["message"].lower()

    @patch("deploy_correlation.query_cloudtrail_changes")
    @patch("deploy_correlation.query_kubernetes_changes")
    def test_partial_failure_returns_success_with_metadata(self, mock_k8s, mock_ct):
        """One source failing returns partial results with unavailable metadata (Req 4.8)."""
        mock_ct.return_value = ([
            {
                "id": "ct-abc123",
                "type": "deployment",
                "source": "cloudtrail",
                "timestamp": "2024-01-15T09:55:00+00:00",
                "actor": "arn:aws:iam::123:role/deploy-role",
                "description": "UpdateService on ECS",
                "affectedResource": "arn:aws:ecs:us-east-1:123:service/prod/payments",
                "details": {"eventName": "UpdateService"},
            }
        ], None)
        mock_k8s.return_value = ([], "connection timeout")

        result = dc.run_deploy_correlation({
            "affectedResources": [
                {"type": "arn", "value": "arn:aws:ecs:us-east-1:123:service/prod/payments", "displayName": "payments"}
            ],
            "incidentTime": "2024-01-15T10:00:00Z",
            "lookbackWindow": "24h",
        })

        assert result["status"] == "success"
        assert len(result["data"]["changes"]) == 1
        assert "kubernetes" in result["data"]["unavailableSources"]
        assert result["data"]["reducedConfidence"] is True

    @patch("deploy_correlation.query_cloudtrail_changes")
    @patch("deploy_correlation.query_kubernetes_changes")
    def test_changes_scored_and_sorted(self, mock_k8s, mock_ct):
        """Changes are scored and sorted by correlation score descending."""
        mock_ct.return_value = ([
            {
                "id": "ct-1",
                "type": "deployment",
                "source": "cloudtrail",
                "timestamp": "2024-01-15T09:55:00+00:00",  # 5 min before
                "actor": "deploy-role",
                "description": "UpdateService",
                "affectedResource": "arn:aws:ecs:us-east-1:123:service/prod/payments",
                "details": {},
            },
            {
                "id": "ct-2",
                "type": "config_change",
                "source": "cloudtrail",
                "timestamp": "2024-01-14T10:00:00+00:00",  # 24h before
                "actor": "admin",
                "description": "PutParameter",
                "affectedResource": "arn:aws:ssm:us-east-1:123:parameter/other",
                "details": {},
            },
        ], None)
        mock_k8s.return_value = ([], None)

        result = dc.run_deploy_correlation({
            "affectedResources": [
                {"type": "arn", "value": "arn:aws:ecs:us-east-1:123:service/prod/payments", "displayName": "payments"}
            ],
            "incidentTime": "2024-01-15T10:00:00Z",
            "lookbackWindow": "24h",
        })

        assert result["status"] == "success"
        changes = result["data"]["changes"]
        assert len(changes) == 2
        # First change should have higher score (closer in time + resource overlap)
        assert changes[0]["correlationScore"] > changes[1]["correlationScore"]
        assert changes[0]["resourceOverlap"] is True
        assert changes[0]["temporalProximity"] > 0.99
