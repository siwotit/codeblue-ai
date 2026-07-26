"""Unit tests for incident_summary.py skill."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

# Ensure the project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# Import the skill functions from the hyphenated directory
sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent.parent / "skills" / "incident-summary-format"),
)
from incident_summary import (
    format_incident_report,
    _build_plain_text_fallback,
    _build_header_section,
    _build_severity_section,
    _build_blast_radius_section,
    _build_hypothesis_section,
    _build_evidence_section,
    _build_next_steps_section,
    _build_escalation_section,
    _build_metadata_footer,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_report() -> dict[str, Any]:
    """Create a minimal valid IncidentReport."""
    return {
        "incidentId": "inc-12345",
        "generatedAt": "2024-01-15T10:30:00Z",
        "processingDuration": "12.5s",
        "alert": {
            "id": "alert-001",
            "source": "cloudwatch",
            "severity": "high",
            "title": "High CPU on web-server-1",
            "firedAt": "2024-01-15T10:29:00Z",
            "region": "us-east-1",
        },
        "severity": "high",
        "blastRadius": {
            "summary": "3 services affected in us-east-1",
            "affectedServices": ["web-api", "auth-service", "payment-gateway"],
            "affectedNamespaces": ["production"],
            "affectedNodes": ["node-1", "node-2"],
            "impactedPodCount": 12,
            "region": "us-east-1",
        },
        "hypotheses": [
            {
                "rank": 1,
                "title": "Memory leak in auth-service after deploy",
                "description": "Recent deployment introduced a memory leak causing OOM kills",
                "confidence": 0.82,
                "supportingEvidence": [
                    {
                        "claim": "Memory usage increased 3x after deploy",
                        "source": "cloudwatch",
                        "consoleUrl": "https://console.aws.amazon.com/cloudwatch/...",
                    }
                ],
                "contradictingEvidence": [],
                "suggestedVerification": [
                    "Check auth-service pod memory usage",
                    "Review recent deployment changes",
                ],
            }
        ],
        "confidence": 0.82,
        "recommendedActions": [
            "Roll back auth-service to previous version",
            "Scale up replicas to handle load",
        ],
        "escalation": {
            "escalate": True,
            "reason": "Critical severity with multi-service blast radius",
            "urgency": "page_now",
            "suggestedResponders": ["oncall-platform", "auth-team-lead"],
        },
        "evidenceLinks": [
            {
                "claim": "CPU spike at 10:28 UTC",
                "source": "cloudwatch",
                "consoleUrl": "https://console.aws.amazon.com/cloudwatch/metrics",
            },
            {
                "claim": "Deploy at 10:25 UTC",
                "source": "cloudtrail",
                "verificationCommand": "aws cloudtrail lookup-events --lookup-attributes ...",
            },
        ],
    }


def _full_report() -> dict[str, Any]:
    """Create a full IncidentReport with all fields."""
    report = _minimal_report()
    report["hypotheses"].append(
        {
            "rank": 2,
            "title": "Network partition in AZ-b",
            "description": "Availability zone b experiencing connectivity issues",
            "confidence": 0.45,
            "supportingEvidence": [],
            "contradictingEvidence": [],
            "suggestedVerification": ["Check VPC flow logs"],
        }
    )
    return report


@pytest.fixture(autouse=True)
def set_slack_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the required CODEBLUE_SLACK_CHANNEL env var for all tests."""
    monkeypatch.setenv("CODEBLUE_SLACK_CHANNEL", "#incidents")


# ---------------------------------------------------------------------------
# Tests: format_incident_report
# ---------------------------------------------------------------------------


class TestFormatIncidentReport:
    """Tests for the main format_incident_report function."""

    def test_success_response_structure(self) -> None:
        """A valid report produces a success SkillResponse with correct shape."""
        result = format_incident_report(_minimal_report())

        assert result["status"] == "success"
        assert result["message"] == ""
        assert result["data"] is not None

        data = result["data"]
        assert data["channel"] == "#incidents"
        assert isinstance(data["text"], str)
        assert isinstance(data["blocks"], list)
        assert isinstance(data["metadata"], dict)

    def test_metadata_fields(self) -> None:
        """Metadata includes incidentId, severity, and generatedBy."""
        result = format_incident_report(_minimal_report())
        metadata = result["data"]["metadata"]

        assert metadata["incidentId"] == "inc-12345"
        assert metadata["severity"] == "high"
        assert metadata["generatedBy"] == "codeblue-ai"

    def test_missing_incident_id_returns_error(self) -> None:
        """Missing incidentId produces error response."""
        report = _minimal_report()
        del report["incidentId"]

        result = format_incident_report(report)
        assert result["status"] == "error"
        assert "incidentId" in result["message"]

    def test_missing_alert_returns_error(self) -> None:
        """Missing alert produces error response."""
        report = _minimal_report()
        del report["alert"]

        result = format_incident_report(report)
        assert result["status"] == "error"
        assert "alert" in result["message"]

    def test_missing_severity_returns_error(self) -> None:
        """Missing severity produces error response."""
        report = _minimal_report()
        del report["severity"]

        result = format_incident_report(report)
        assert result["status"] == "error"
        assert "severity" in result["message"]

    def test_missing_channel_env_returns_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing CODEBLUE_SLACK_CHANNEL env var produces error."""
        monkeypatch.delenv("CODEBLUE_SLACK_CHANNEL", raising=False)

        result = format_incident_report(_minimal_report())
        assert result["status"] == "error"
        assert "channel" in result["message"].lower()

    def test_blocks_contain_required_types(self) -> None:
        """Output blocks contain header, section, context, and divider types."""
        result = format_incident_report(_minimal_report())
        blocks = result["data"]["blocks"]
        block_types = {b["type"] for b in blocks}

        assert "header" in block_types
        assert "section" in block_types
        assert "context" in block_types
        assert "divider" in block_types

    def test_blocks_start_with_header(self) -> None:
        """First block is the header."""
        result = format_incident_report(_minimal_report())
        blocks = result["data"]["blocks"]
        assert blocks[0]["type"] == "header"

    def test_blocks_end_with_context_footer(self) -> None:
        """Last block is the metadata footer context."""
        result = format_incident_report(_minimal_report())
        blocks = result["data"]["blocks"]
        assert blocks[-1]["type"] == "context"

    def test_no_blast_radius_omits_section(self) -> None:
        """When blastRadius is None, that section is omitted."""
        report = _minimal_report()
        report["blastRadius"] = None

        result = format_incident_report(report)
        blocks = result["data"]["blocks"]
        all_text = " ".join(
            b.get("text", {}).get("text", "")
            for b in blocks
            if b["type"] == "section"
        )
        assert "Blast Radius" not in all_text

    def test_no_escalation_omits_section(self) -> None:
        """When escalation is None, that section is omitted."""
        report = _minimal_report()
        report["escalation"] = None

        result = format_incident_report(report)
        blocks = result["data"]["blocks"]
        all_text = " ".join(
            b.get("text", {}).get("text", "")
            for b in blocks
            if b["type"] == "section"
        )
        assert "ESCALATION" not in all_text
        assert "Notify Channel" not in all_text

    def test_no_hypotheses_produces_fallback_text(self) -> None:
        """When hypotheses is empty, section states no hypothesis generated."""
        report = _minimal_report()
        report["hypotheses"] = []
        report["confidence"] = 0.0

        result = format_incident_report(report)
        blocks = result["data"]["blocks"]
        section_texts = [
            b["text"]["text"]
            for b in blocks
            if b["type"] == "section"
        ]
        assert any("No root cause hypothesis" in t for t in section_texts)


# ---------------------------------------------------------------------------
# Tests: Severity emoji mapping
# ---------------------------------------------------------------------------


class TestSeverityEmoji:
    """Tests for severity-to-emoji mapping in header and severity sections."""

    @pytest.mark.parametrize(
        "severity,expected_emoji",
        [
            ("critical", "🔴"),
            ("high", "🟠"),
            ("medium", "🟡"),
            ("low", "🟢"),
        ],
    )
    def test_header_contains_correct_emoji(self, severity: str, expected_emoji: str) -> None:
        """Header block contains the correct emoji for each severity."""
        report = _minimal_report()
        report["severity"] = severity

        blocks = _build_header_section(report)
        header_text = blocks[0]["text"]["text"]
        assert expected_emoji in header_text

    @pytest.mark.parametrize(
        "severity,expected_emoji",
        [
            ("critical", "🔴"),
            ("high", "🟠"),
            ("medium", "🟡"),
            ("low", "🟢"),
        ],
    )
    def test_severity_section_contains_correct_emoji(self, severity: str, expected_emoji: str) -> None:
        """Severity section contains the correct emoji."""
        report = _minimal_report()
        report["severity"] = severity

        blocks = _build_severity_section(report)
        section_text = blocks[0]["text"]["text"]
        assert expected_emoji in section_text


# ---------------------------------------------------------------------------
# Tests: Plain-text fallback
# ---------------------------------------------------------------------------


class TestPlainTextFallback:
    """Tests for the plain-text fallback summary."""

    def test_contains_severity(self) -> None:
        """Fallback includes severity in brackets."""
        report = _minimal_report()
        fallback = _build_plain_text_fallback(report)
        assert "[HIGH]" in fallback

    def test_contains_alert_title(self) -> None:
        """Fallback includes the alert title."""
        report = _minimal_report()
        fallback = _build_plain_text_fallback(report)
        assert "High CPU on web-server-1" in fallback

    def test_contains_blast_radius_summary(self) -> None:
        """Fallback includes blast radius summary."""
        report = _minimal_report()
        fallback = _build_plain_text_fallback(report)
        assert "3 services affected" in fallback

    def test_contains_hypothesis(self) -> None:
        """Fallback includes likely cause."""
        report = _minimal_report()
        fallback = _build_plain_text_fallback(report)
        assert "Memory leak" in fallback

    def test_contains_escalation(self) -> None:
        """Fallback includes escalation info."""
        report = _minimal_report()
        fallback = _build_plain_text_fallback(report)
        assert "page_now" in fallback

    def test_contains_codeblue_identifier(self) -> None:
        """Fallback includes CodeBlue AI identifier."""
        report = _minimal_report()
        fallback = _build_plain_text_fallback(report)
        assert "CodeBlue AI" in fallback

    def test_contains_incident_id(self) -> None:
        """Fallback includes incident ID."""
        report = _minimal_report()
        fallback = _build_plain_text_fallback(report)
        assert "inc-12345" in fallback

    def test_contains_processing_duration(self) -> None:
        """Fallback includes processing duration."""
        report = _minimal_report()
        fallback = _build_plain_text_fallback(report)
        assert "12.5s" in fallback


# ---------------------------------------------------------------------------
# Tests: Escalation formatting
# ---------------------------------------------------------------------------


class TestEscalationSection:
    """Tests for escalation section formatting."""

    @pytest.mark.parametrize(
        "urgency,expected_prefix",
        [
            ("page_now", "🚨"),
            ("notify_channel", "⚡"),
            ("business_hours", "📋"),
        ],
    )
    def test_urgency_emoji_mapping(self, urgency: str, expected_prefix: str) -> None:
        """Each urgency level maps to the correct emoji prefix."""
        report = _minimal_report()
        report["escalation"]["urgency"] = urgency

        blocks = _build_escalation_section(report)
        text = blocks[0]["text"]["text"]
        assert expected_prefix in text

    def test_includes_reason(self) -> None:
        """Escalation section includes the reason."""
        report = _minimal_report()
        blocks = _build_escalation_section(report)
        text = blocks[0]["text"]["text"]
        assert "Critical severity with multi-service blast radius" in text

    def test_includes_suggested_responders(self) -> None:
        """Escalation section includes suggested responders."""
        report = _minimal_report()
        blocks = _build_escalation_section(report)
        text = blocks[0]["text"]["text"]
        assert "oncall-platform" in text
        assert "auth-team-lead" in text


# ---------------------------------------------------------------------------
# Tests: Hypothesis section
# ---------------------------------------------------------------------------


class TestHypothesisSection:
    """Tests for hypothesis section formatting."""

    def test_low_confidence_caveat(self) -> None:
        """Confidence < 0.5 adds low confidence warning."""
        report = _minimal_report()
        report["hypotheses"][0]["confidence"] = 0.3
        report["confidence"] = 0.3

        blocks = _build_hypothesis_section(report)
        text = blocks[0]["text"]["text"]
        assert "⚠️ Low confidence" in text

    def test_high_confidence_no_caveat(self) -> None:
        """Confidence >= 0.5 does not add low confidence warning."""
        report = _minimal_report()
        blocks = _build_hypothesis_section(report)
        text = blocks[0]["text"]["text"]
        assert "⚠️" not in text

    def test_close_hypotheses_shows_alternative(self) -> None:
        """When top two hypotheses are within 0.15 confidence, show both."""
        report = _full_report()
        # Set close confidences
        report["hypotheses"][0]["confidence"] = 0.60
        report["hypotheses"][1]["confidence"] = 0.50

        blocks = _build_hypothesis_section(report)
        text = blocks[0]["text"]["text"]
        assert "Alternative" in text
        assert "Network partition" in text


# ---------------------------------------------------------------------------
# Tests: Evidence section
# ---------------------------------------------------------------------------


class TestEvidenceSection:
    """Tests for evidence links section formatting."""

    def test_console_url_creates_link(self) -> None:
        """Evidence with consoleUrl creates clickable link."""
        report = _minimal_report()
        blocks = _build_evidence_section(report)
        text = blocks[0]["text"]["text"]
        assert "View in Console" in text

    def test_verification_command_uses_code(self) -> None:
        """Evidence with verificationCommand uses inline code."""
        report = _minimal_report()
        blocks = _build_evidence_section(report)
        text = blocks[0]["text"]["text"]
        assert "`aws cloudtrail" in text

    def test_max_five_items(self) -> None:
        """At most 5 evidence items are shown."""
        report = _minimal_report()
        report["evidenceLinks"] = [
            {"claim": f"Claim {i}", "source": "test"}
            for i in range(10)
        ]

        blocks = _build_evidence_section(report)
        text = blocks[0]["text"]["text"]
        # Count bullet points
        assert text.count("•") == 5

    def test_falls_back_to_hypothesis_evidence(self) -> None:
        """When evidenceLinks is empty, uses top hypothesis supportingEvidence."""
        report = _minimal_report()
        report["evidenceLinks"] = []

        blocks = _build_evidence_section(report)
        assert len(blocks) > 0
        text = blocks[0]["text"]["text"]
        assert "Memory usage increased" in text
