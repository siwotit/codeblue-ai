"""Unit tests for the escalation_decision skill."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Add the skill to the path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "escalation-decision"))

from escalation_decision import (
    _increase_urgency,
    _is_multi_service,
    _is_outside_business_hours,
    _has_tier1_service,
    make_escalation_decision,
)


# ---------------------------------------------------------------------------
# Step 1: Severity baseline mapping (Requirement 8.1)
# ---------------------------------------------------------------------------


class TestSeverityBaseline:
    """Test severity-to-urgency baseline mapping."""

    def test_critical_maps_to_page_now(self):
        result = make_escalation_decision({
            "severity": "critical",
            "blastRadius": {"affectedServices": ["svc-a"], "region": "us-east-1"},
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
        })
        assert result["status"] == "success"
        assert result["data"]["urgency"] == "page_now"

    def test_high_maps_to_notify_channel(self):
        result = make_escalation_decision({
            "severity": "high",
            "blastRadius": {"affectedServices": ["svc-a"], "region": "us-east-1"},
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
        })
        assert result["status"] == "success"
        assert result["data"]["urgency"] == "notify_channel"

    def test_medium_maps_to_business_hours(self):
        result = make_escalation_decision({
            "severity": "medium",
            "blastRadius": {"affectedServices": ["svc-a"], "region": "us-east-1"},
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
        })
        assert result["status"] == "success"
        assert result["data"]["urgency"] == "business_hours"

    def test_low_maps_to_business_hours(self):
        result = make_escalation_decision({
            "severity": "low",
            "blastRadius": {"affectedServices": ["svc-a"], "region": "us-east-1"},
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
        })
        assert result["status"] == "success"
        assert result["data"]["urgency"] == "business_hours"


# ---------------------------------------------------------------------------
# Step 2: Blast radius escalation (Requirement 8.2)
# ---------------------------------------------------------------------------


class TestBlastRadiusEscalation:
    """Test multi-service blast radius increases urgency by one level."""

    def test_multi_service_high_escalates_to_page_now(self):
        """High + multi-service → page_now."""
        result = make_escalation_decision({
            "severity": "high",
            "blastRadius": {
                "affectedServices": ["svc-a", "svc-b", "svc-c"],
                "region": "us-east-1",
            },
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
        })
        assert result["data"]["urgency"] == "page_now"

    def test_medium_multi_service_escalates_to_notify_channel(self):
        """Medium + multi-service → notify_channel."""
        result = make_escalation_decision({
            "severity": "medium",
            "blastRadius": {
                "affectedServices": ["svc-a", "svc-b"],
                "region": "us-east-1",
            },
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
        })
        assert result["data"]["urgency"] == "notify_channel"

    def test_single_service_no_escalation(self):
        """Single service does not trigger blast radius escalation."""
        result = make_escalation_decision({
            "severity": "high",
            "blastRadius": {
                "affectedServices": ["svc-a"],
                "region": "us-east-1",
            },
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
        })
        assert result["data"]["urgency"] == "notify_channel"

    def test_empty_services_no_escalation(self):
        """Empty affectedServices does not trigger escalation."""
        result = make_escalation_decision({
            "severity": "high",
            "blastRadius": {
                "affectedServices": [],
                "region": "us-east-1",
            },
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
        })
        assert result["data"]["urgency"] == "notify_channel"


# ---------------------------------------------------------------------------
# Step 3: Time-of-day escalation (Requirement 8.2)
# ---------------------------------------------------------------------------


class TestTimeOfDayEscalation:
    """Test off-hours escalation with high+ severity."""

    def test_high_severity_off_hours_escalates(self):
        """High severity outside business hours → page_now."""
        result = make_escalation_decision({
            "severity": "high",
            "blastRadius": {"affectedServices": ["svc-a"], "region": "us-east-1"},
            "currentTimeUtc": "2024-01-15T03:00:00Z",  # 3 AM UTC
            "teamTimezone": "UTC",
        })
        assert result["data"]["urgency"] == "page_now"

    def test_high_severity_in_hours_no_escalation(self):
        """High severity during business hours → stays at notify_channel."""
        result = make_escalation_decision({
            "severity": "high",
            "blastRadius": {"affectedServices": ["svc-a"], "region": "us-east-1"},
            "currentTimeUtc": "2024-01-15T12:00:00Z",  # noon UTC
            "teamTimezone": "UTC",
        })
        assert result["data"]["urgency"] == "notify_channel"

    def test_medium_severity_off_hours_no_escalation(self):
        """Medium severity outside business hours does NOT escalate."""
        result = make_escalation_decision({
            "severity": "medium",
            "blastRadius": {"affectedServices": ["svc-a"], "region": "us-east-1"},
            "currentTimeUtc": "2024-01-15T03:00:00Z",
            "teamTimezone": "UTC",
        })
        assert result["data"]["urgency"] == "business_hours"

    def test_timezone_conversion(self):
        """Off-hours in team timezone but not UTC."""
        # 20:00 UTC = 15:00 America/New_York (in EST, -5h) → in hours
        result = make_escalation_decision({
            "severity": "high",
            "blastRadius": {"affectedServices": ["svc-a"], "region": "us-east-1"},
            "currentTimeUtc": "2024-01-15T20:00:00Z",
            "teamTimezone": "America/New_York",
        })
        assert result["data"]["urgency"] == "notify_channel"

    def test_boundary_9am_is_in_hours(self):
        """09:00 is within business hours."""
        result = make_escalation_decision({
            "severity": "high",
            "blastRadius": {"affectedServices": ["svc-a"], "region": "us-east-1"},
            "currentTimeUtc": "2024-01-15T09:00:00Z",
            "teamTimezone": "UTC",
        })
        assert result["data"]["urgency"] == "notify_channel"

    def test_boundary_17_is_out_of_hours(self):
        """17:00 is outside business hours (exclusive)."""
        result = make_escalation_decision({
            "severity": "high",
            "blastRadius": {"affectedServices": ["svc-a"], "region": "us-east-1"},
            "currentTimeUtc": "2024-01-15T17:00:00Z",
            "teamTimezone": "UTC",
        })
        assert result["data"]["urgency"] == "page_now"


# ---------------------------------------------------------------------------
# Step 4: Service criticality (Requirement 8.5)
# ---------------------------------------------------------------------------


class TestServiceCriticalityEscalation:
    """Test tier-1 services force page_now at high+ severity."""

    def test_tier1_high_forces_page_now(self):
        result = make_escalation_decision({
            "severity": "high",
            "blastRadius": {
                "affectedServices": ["payments-service"],
                "region": "us-east-1",
            },
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
            "serviceCriticality": {"payments-service": "tier-1"},
        })
        assert result["data"]["urgency"] == "page_now"

    def test_tier1_critical_stays_page_now(self):
        result = make_escalation_decision({
            "severity": "critical",
            "blastRadius": {
                "affectedServices": ["payments-service"],
                "region": "us-east-1",
            },
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
            "serviceCriticality": {"payments-service": "tier-1"},
        })
        assert result["data"]["urgency"] == "page_now"

    def test_tier1_medium_no_override(self):
        """Tier-1 with medium severity does NOT force page_now."""
        result = make_escalation_decision({
            "severity": "medium",
            "blastRadius": {
                "affectedServices": ["payments-service"],
                "region": "us-east-1",
            },
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
            "serviceCriticality": {"payments-service": "tier-1"},
        })
        assert result["data"]["urgency"] == "business_hours"

    def test_tier2_no_override(self):
        """Tier-2 does not trigger override."""
        result = make_escalation_decision({
            "severity": "high",
            "blastRadius": {
                "affectedServices": ["internal-service"],
                "region": "us-east-1",
            },
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
            "serviceCriticality": {"internal-service": "tier-2"},
        })
        assert result["data"]["urgency"] == "notify_channel"

    def test_null_service_criticality_skipped(self):
        """Null serviceCriticality skips step 4."""
        result = make_escalation_decision({
            "severity": "high",
            "blastRadius": {
                "affectedServices": ["payments-service"],
                "region": "us-east-1",
            },
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
            "serviceCriticality": None,
        })
        assert result["data"]["urgency"] == "notify_channel"


# ---------------------------------------------------------------------------
# Step 5: Escalation flag (Requirement 8.3)
# ---------------------------------------------------------------------------


class TestEscalationFlag:
    """Test escalate boolean set correctly."""

    def test_page_now_escalates(self):
        result = make_escalation_decision({
            "severity": "critical",
            "blastRadius": {"affectedServices": ["svc"], "region": "us-east-1"},
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
        })
        assert result["data"]["escalate"] is True

    def test_notify_channel_escalates(self):
        result = make_escalation_decision({
            "severity": "high",
            "blastRadius": {"affectedServices": ["svc"], "region": "us-east-1"},
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
        })
        assert result["data"]["escalate"] is True

    def test_business_hours_no_escalation(self):
        result = make_escalation_decision({
            "severity": "medium",
            "blastRadius": {"affectedServices": ["svc"], "region": "us-east-1"},
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
        })
        assert result["data"]["escalate"] is False


# ---------------------------------------------------------------------------
# Reason generation (Requirement 8.4)
# ---------------------------------------------------------------------------


class TestReasonGeneration:
    """Test that reason is non-empty and references severity."""

    def test_reason_non_empty(self):
        result = make_escalation_decision({
            "severity": "high",
            "blastRadius": {"affectedServices": ["svc"], "region": "us-east-1"},
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
        })
        assert result["data"]["reason"]
        assert len(result["data"]["reason"]) > 0

    def test_reason_references_severity(self):
        result = make_escalation_decision({
            "severity": "high",
            "blastRadius": {"affectedServices": ["svc"], "region": "us-east-1"},
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
        })
        assert "high" in result["data"]["reason"].lower() or "High" in result["data"]["reason"]

    def test_reason_mentions_unavailable_sources(self):
        result = make_escalation_decision({
            "severity": "high",
            "blastRadius": {"affectedServices": ["svc"], "region": "us-east-1"},
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
            "unavailableSources": ["metricDeviation", "logTriage"],
        })
        assert "metricDeviation" in result["data"]["reason"]
        assert "logTriage" in result["data"]["reason"]


# ---------------------------------------------------------------------------
# Incomplete data handling (Requirement 8.6)
# ---------------------------------------------------------------------------


class TestIncompleteDataHandling:
    """Test graceful handling of missing data."""

    def test_missing_severity_returns_error(self):
        result = make_escalation_decision({
            "blastRadius": {"affectedServices": ["svc"], "region": "us-east-1"},
        })
        assert result["status"] == "error"
        assert "severity" in result["message"]

    def test_missing_blast_radius_uses_baseline(self):
        result = make_escalation_decision({
            "severity": "high",
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
        })
        assert result["status"] == "success"
        assert result["data"]["urgency"] == "notify_channel"

    def test_invalid_timezone_assumes_off_hours(self):
        """Invalid timezone errs on the side of caution."""
        result = make_escalation_decision({
            "severity": "high",
            "blastRadius": {"affectedServices": ["svc"], "region": "us-east-1"},
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "Invalid/Timezone",
        })
        # Should assume outside hours → escalate
        assert result["data"]["urgency"] == "page_now"

    def test_empty_input_error(self):
        result = make_escalation_decision({})
        assert result["status"] == "error"

    def test_invalid_severity_error(self):
        result = make_escalation_decision({"severity": "extreme"})
        assert result["status"] == "error"
        assert "invalid severity" in result["message"].lower()


# ---------------------------------------------------------------------------
# Combined escalation scenarios
# ---------------------------------------------------------------------------


class TestCombinedScenarios:
    """Test complex scenarios with multiple escalation factors."""

    def test_full_escalation_scenario(self):
        """Critical + multi-service + off-hours + tier-1 → page_now."""
        result = make_escalation_decision({
            "severity": "critical",
            "blastRadius": {
                "affectedServices": ["payments-service", "checkout-service"],
                "affectedNodes": ["node-1", "node-2"],
                "impactedPodCount": 12,
                "region": "us-east-1",
            },
            "currentTimeUtc": "2024-01-15T03:42:00Z",
            "teamTimezone": "America/New_York",
            "serviceCriticality": {
                "payments-service": "tier-1",
                "checkout-service": "tier-1",
            },
            "unavailableSources": ["metricDeviation"],
        })
        assert result["status"] == "success"
        assert result["data"]["urgency"] == "page_now"
        assert result["data"]["escalate"] is True
        assert result["data"]["reason"]
        assert result["data"]["suggestedResponders"]

    def test_urgency_never_decreases(self):
        """Urgency can only increase through steps, never decrease."""
        # Critical starts at page_now — even if no other factors, stays page_now
        result = make_escalation_decision({
            "severity": "critical",
            "blastRadius": {"affectedServices": [], "region": "us-east-1"},
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
        })
        assert result["data"]["urgency"] == "page_now"

    def test_suggested_responders_capped_at_3(self):
        """suggestedResponders limited to at most 3 entries."""
        result = make_escalation_decision({
            "severity": "high",
            "blastRadius": {
                "affectedServices": ["svc-a", "svc-b", "svc-c", "svc-d"],
                "region": "us-east-1",
            },
            "currentTimeUtc": "2024-01-15T12:00:00Z",
            "teamTimezone": "UTC",
            "serviceCriticality": {
                "svc-a": "tier-1",
                "svc-b": "tier-1",
                "svc-c": "tier-1",
                "svc-d": "tier-1",
            },
        })
        assert len(result["data"]["suggestedResponders"]) <= 3
