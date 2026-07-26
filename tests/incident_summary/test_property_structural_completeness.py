"""Property test: Incident summary structural completeness.

**Validates: Requirements 7.1, 7.3, 7.7**

Property 15: For any valid IncidentReport, verify all required sections
are present in formatted output:
1. Output has blocks containing: header, section, divider, context types
2. Plain-text fallback (text field) is non-empty
3. Metadata includes incidentId, severity, generatedBy="codeblue-ai" (Req 7.7)
4. Block Kit has header with severity emoji (Req 7.3)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

# Ensure the project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# Import the skill module from the hyphenated directory
sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent.parent / "skills" / "incident-summary-format"),
)
from incident_summary import format_incident_report, SEVERITY_EMOJI


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

SEVERITIES = st.sampled_from(["critical", "high", "medium", "low"])
SOURCES = st.sampled_from(["cloudwatch", "grafana", "kubernetes"])


@st.composite
def incident_reports(draw: st.DrawFn) -> dict[str, Any]:
    """Generate arbitrary valid IncidentReport dicts."""
    severity = draw(SEVERITIES)
    incident_id = draw(st.text(min_size=1, max_size=50, alphabet=st.characters(
        whitelist_categories=("L", "N", "Pd"),
    )))
    assume(incident_id.strip() != "")

    alert_title = draw(st.text(min_size=1, max_size=100, alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "Z"),
    )))
    assume(alert_title.strip() != "")

    source = draw(SOURCES)
    confidence = draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False))

    # Build hypotheses (0 to 3)
    num_hypotheses = draw(st.integers(min_value=0, max_value=3))
    hypotheses = []
    for rank in range(1, num_hypotheses + 1):
        hyp_confidence = draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
        hypotheses.append({
            "rank": rank,
            "title": draw(st.text(min_size=1, max_size=80, alphabet=st.characters(
                whitelist_categories=("L", "N", "P", "Z"),
            ))),
            "description": draw(st.text(min_size=0, max_size=200, alphabet=st.characters(
                whitelist_categories=("L", "N", "P", "Z"),
            ))),
            "confidence": hyp_confidence,
            "supportingEvidence": [
                {
                    "claim": "Test evidence claim",
                    "source": source,
                    "consoleUrl": "https://example.com/console",
                }
            ],
            "contradictingEvidence": [],
            "suggestedVerification": ["Verify step"],
        })

    # Optional blast radius
    has_blast_radius = draw(st.booleans())
    blast_radius = None
    if has_blast_radius:
        blast_radius = {
            "summary": draw(st.text(min_size=1, max_size=100, alphabet=st.characters(
                whitelist_categories=("L", "N", "P", "Z"),
            ))),
            "affectedServices": draw(st.lists(
                st.text(min_size=1, max_size=30, alphabet=st.characters(
                    whitelist_categories=("L", "N", "Pd"),
                )),
                min_size=0, max_size=5,
            )),
            "affectedNamespaces": draw(st.lists(
                st.text(min_size=1, max_size=30, alphabet=st.characters(
                    whitelist_categories=("L", "N", "Pd"),
                )),
                min_size=0, max_size=3,
            )),
            "affectedNodes": draw(st.lists(
                st.text(min_size=1, max_size=30, alphabet=st.characters(
                    whitelist_categories=("L", "N", "Pd"),
                )),
                min_size=0, max_size=3,
            )),
            "impactedPodCount": draw(st.integers(min_value=0, max_value=500)),
            "region": "us-east-1",
        }

    # Optional escalation
    has_escalation = draw(st.booleans())
    escalation = None
    if has_escalation:
        escalation = {
            "escalate": draw(st.booleans()),
            "reason": draw(st.text(min_size=1, max_size=100, alphabet=st.characters(
                whitelist_categories=("L", "N", "P", "Z"),
            ))),
            "urgency": draw(st.sampled_from(["page_now", "notify_channel", "business_hours"])),
            "suggestedResponders": draw(st.lists(
                st.text(min_size=1, max_size=30, alphabet=st.characters(
                    whitelist_categories=("L", "N", "Pd"),
                )),
                min_size=0, max_size=3,
            )),
        }

    # Optional evidence links
    num_evidence = draw(st.integers(min_value=0, max_value=6))
    evidence_links = []
    for _ in range(num_evidence):
        evidence_links.append({
            "claim": draw(st.text(min_size=1, max_size=80, alphabet=st.characters(
                whitelist_categories=("L", "N", "P", "Z"),
            ))),
            "source": draw(SOURCES),
            "consoleUrl": "https://console.example.com/metric",
        })

    return {
        "incidentId": incident_id,
        "generatedAt": "2024-01-15T10:30:00Z",
        "processingDuration": "15.2s",
        "alert": {
            "id": f"alert-{incident_id}",
            "source": source,
            "severity": severity,
            "title": alert_title,
            "firedAt": "2024-01-15T10:29:00Z",
            "region": "us-east-1",
        },
        "severity": severity,
        "blastRadius": blast_radius,
        "hypotheses": hypotheses,
        "confidence": confidence,
        "recommendedActions": draw(st.lists(
            st.text(min_size=1, max_size=80, alphabet=st.characters(
                whitelist_categories=("L", "N", "P", "Z"),
            )),
            min_size=0, max_size=5,
        )),
        "escalation": escalation,
        "evidenceLinks": evidence_links,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def set_slack_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the required CODEBLUE_SLACK_CHANNEL env var for all tests."""
    monkeypatch.setenv("CODEBLUE_SLACK_CHANNEL", "#test-incidents")


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------


class TestIncidentSummaryStructuralCompleteness:
    """Property 15: Incident summary structural completeness.

    **Validates: Requirements 7.1, 7.3, 7.7**
    """

    @given(report=incident_reports())
    @settings(max_examples=100, deadline=None)
    def test_output_contains_required_block_types(self, report: dict[str, Any]) -> None:
        """For any valid IncidentReport, output blocks contain header, section,
        divider, and context types."""
        os.environ["CODEBLUE_SLACK_CHANNEL"] = "#test-incidents"
        result = format_incident_report(report)

        assert result["status"] == "success", f"Unexpected error: {result.get('message')}"

        blocks = result["data"]["blocks"]
        block_types = {b["type"] for b in blocks}

        assert "header" in block_types, "Output must contain a header block"
        assert "section" in block_types, "Output must contain at least one section block"
        assert "divider" in block_types, "Output must contain at least one divider block"
        assert "context" in block_types, "Output must contain a context block (metadata footer)"

    @given(report=incident_reports())
    @settings(max_examples=100, deadline=None)
    def test_plain_text_fallback_is_non_empty(self, report: dict[str, Any]) -> None:
        """For any valid IncidentReport, the plain-text fallback (text field)
        is non-empty."""
        os.environ["CODEBLUE_SLACK_CHANNEL"] = "#test-incidents"
        result = format_incident_report(report)

        assert result["status"] == "success", f"Unexpected error: {result.get('message')}"

        text = result["data"]["text"]
        assert isinstance(text, str), "text field must be a string"
        assert len(text.strip()) > 0, "Plain-text fallback must be non-empty"

    @given(report=incident_reports())
    @settings(max_examples=100, deadline=None)
    def test_metadata_contains_required_fields(self, report: dict[str, Any]) -> None:
        """For any valid IncidentReport, metadata includes incidentId,
        severity, and generatedBy='codeblue-ai' (Req 7.7)."""
        os.environ["CODEBLUE_SLACK_CHANNEL"] = "#test-incidents"
        result = format_incident_report(report)

        assert result["status"] == "success", f"Unexpected error: {result.get('message')}"

        metadata = result["data"]["metadata"]

        assert "incidentId" in metadata, "Metadata must include incidentId"
        assert metadata["incidentId"] == report["incidentId"], (
            "Metadata incidentId must match the report's incidentId"
        )

        assert "severity" in metadata, "Metadata must include severity"
        assert metadata["severity"] == report["severity"], (
            "Metadata severity must match the report's severity"
        )

        assert "generatedBy" in metadata, "Metadata must include generatedBy"
        assert metadata["generatedBy"] == "codeblue-ai", (
            "generatedBy must be 'codeblue-ai'"
        )

    @given(report=incident_reports())
    @settings(max_examples=100, deadline=None)
    def test_header_contains_severity_emoji(self, report: dict[str, Any]) -> None:
        """For any valid IncidentReport, the Block Kit header contains
        the correct severity emoji (Req 7.3)."""
        os.environ["CODEBLUE_SLACK_CHANNEL"] = "#test-incidents"
        result = format_incident_report(report)

        assert result["status"] == "success", f"Unexpected error: {result.get('message')}"

        blocks = result["data"]["blocks"]
        header_blocks = [b for b in blocks if b["type"] == "header"]

        assert len(header_blocks) >= 1, "Must have at least one header block"

        header_text = header_blocks[0]["text"]["text"]
        expected_emoji = SEVERITY_EMOJI.get(report["severity"], "🟡")

        assert expected_emoji in header_text, (
            f"Header must contain severity emoji '{expected_emoji}' for "
            f"severity '{report['severity']}', got: '{header_text}'"
        )
