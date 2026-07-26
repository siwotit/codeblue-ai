"""Incident Summary Format Skill — formats IncidentReport into Slack Block Kit message.

Accepts IncidentReport JSON on stdin.
Returns SkillResponse JSON to stdout wrapping a SlackMessage.

Usage:
    echo '{"incidentId": "...", ...}' | uv run incident_summary.py
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEVERITY_EMOJI: dict[str, str] = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🟢",
}

ESCALATION_URGENCY_FORMAT: dict[str, str] = {
    "page_now": "🚨 *ESCALATION: PAGE NOW*",
    "notify_channel": "⚡ *Notify Channel*",
    "business_hours": "📋 *Business Hours*",
}

MAX_EVIDENCE_ITEMS = 5


# ---------------------------------------------------------------------------
# Block Kit builders
# ---------------------------------------------------------------------------


def _header_block(text: str) -> dict[str, Any]:
    """Create a Block Kit header block."""
    # Slack header text max is 150 chars
    return {
        "type": "header",
        "text": {"type": "plain_text", "text": text[:150], "emoji": True},
    }


def _section_block(mrkdwn_text: str) -> dict[str, Any]:
    """Create a Block Kit section block with mrkdwn text."""
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": mrkdwn_text},
    }


def _context_block(text: str) -> dict[str, Any]:
    """Create a Block Kit context block."""
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": text}],
    }


def _divider_block() -> dict[str, Any]:
    """Create a Block Kit divider block."""
    return {"type": "divider"}


# ---------------------------------------------------------------------------
# Section formatters
# ---------------------------------------------------------------------------


def _build_header_section(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the header section: emoji + alert title."""
    severity = report.get("severity", "medium")
    emoji = SEVERITY_EMOJI.get(severity, "🟡")
    alert = report.get("alert", {})
    title = alert.get("title", "Incident Alert")
    header_text = f"{emoji} {title}"
    return [_header_block(header_text)]


def _build_severity_section(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Build severity & status section."""
    severity = report.get("severity", "medium")
    emoji = SEVERITY_EMOJI.get(severity, "🟡")
    confidence = report.get("confidence", 0.0)
    confidence_pct = int(confidence * 100)
    incident_id = report.get("incidentId", "unknown")
    processing_duration = report.get("processingDuration", "unknown")

    text = (
        f"*Severity:* {emoji} {severity.upper()} | "
        f"*Confidence:* {confidence_pct}%\n"
        f"*Incident ID:* `{incident_id}` | "
        f"*Processed in:* {processing_duration}"
    )
    return [_section_block(text)]


def _build_blast_radius_section(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Build blast radius section. Omit if blastRadius is missing."""
    blast_radius = report.get("blastRadius")
    if not blast_radius:
        return []

    summary = blast_radius.get("summary", "Unknown")
    affected_services = blast_radius.get("affectedServices", [])
    affected_namespaces = blast_radius.get("affectedNamespaces", [])
    affected_nodes = blast_radius.get("affectedNodes", [])
    impacted_pod_count = blast_radius.get("impactedPodCount", 0)

    lines = [f"*Blast Radius:* {summary}"]

    if affected_services:
        lines.append(f"• *Services:* {', '.join(affected_services)}")
    if affected_namespaces:
        lines.append(f"• *Namespaces:* {', '.join(affected_namespaces)}")
    if affected_nodes:
        lines.append(f"• *Nodes:* {', '.join(affected_nodes)}")
    if impacted_pod_count > 0:
        lines.append(f"• *Impacted Pods:* {impacted_pod_count}")

    return [_section_block("\n".join(lines))]


def _build_hypothesis_section(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Build root cause hypothesis section."""
    hypotheses = report.get("hypotheses", [])
    confidence = report.get("confidence", 0.0)
    confidence_pct = int(confidence * 100)

    if not hypotheses:
        text = "*Most Likely Cause:* No root cause hypothesis could be generated (0% confidence)"
        return [_section_block(text)]

    top = hypotheses[0]
    title = top.get("title", "Unknown")
    description = top.get("description", "")
    top_confidence = top.get("confidence", confidence)
    top_confidence_pct = int(top_confidence * 100)

    lines = [f"*Most Likely Cause:* {title} ({top_confidence_pct}% confidence)"]

    if description:
        lines.append(f"_{description}_")

    if top_confidence < 0.5:
        lines.append("⚠️ Low confidence — multiple causes possible")

    # If second hypothesis is within 0.15 of top, mention it
    if len(hypotheses) > 1:
        second = hypotheses[1]
        second_confidence = second.get("confidence", 0.0)
        if abs(top_confidence - second_confidence) <= 0.15:
            second_title = second.get("title", "Unknown")
            second_pct = int(second_confidence * 100)
            lines.append(
                f"\n*Alternative:* {second_title} ({second_pct}% confidence)"
            )

    return [_section_block("\n".join(lines))]


def _build_evidence_section(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Build evidence links section. Omit if no evidence."""
    evidence_links = report.get("evidenceLinks", [])

    # Also gather from top hypothesis supporting evidence if no top-level links
    if not evidence_links:
        hypotheses = report.get("hypotheses", [])
        if hypotheses:
            evidence_links = hypotheses[0].get("supportingEvidence", [])

    if not evidence_links:
        return []

    lines = ["*Evidence:*"]
    for item in evidence_links[:MAX_EVIDENCE_ITEMS]:
        claim = item.get("claim", "")
        console_url = item.get("consoleUrl")
        verification_cmd = item.get("verificationCommand")

        if console_url:
            lines.append(f"• {claim} — <{console_url}|View in Console>")
        elif verification_cmd:
            lines.append(f"• {claim} — `{verification_cmd}`")
        else:
            source = item.get("source", "")
            lines.append(f"• {claim} — _{source}_")

    return [_section_block("\n".join(lines))]


def _build_next_steps_section(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Build recommended next steps section."""
    actions = report.get("recommendedActions", [])

    # Also include suggested verification from top hypothesis
    hypotheses = report.get("hypotheses", [])
    verification_steps: list[str] = []
    if hypotheses:
        verification_steps = hypotheses[0].get("suggestedVerification", [])

    all_steps = list(actions)
    for step in verification_steps:
        if step not in all_steps:
            all_steps.append(step)

    if not all_steps:
        return []

    lines = ["*Recommended Next Steps:*"]
    for i, step in enumerate(all_steps, 1):
        lines.append(f"{i}. {step}")

    return [_section_block("\n".join(lines))]


def _build_escalation_section(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Build escalation section. Omit if no escalation decision."""
    escalation = report.get("escalation")
    if not escalation:
        return []

    urgency = escalation.get("urgency", "business_hours")
    reason = escalation.get("reason", "")
    suggested_responders = escalation.get("suggestedResponders", [])

    prefix = ESCALATION_URGENCY_FORMAT.get(urgency, "📋 *Business Hours*")
    lines = [f"{prefix} — {reason}"]

    if suggested_responders:
        lines.append(f"*Suggested Responders:* {', '.join(suggested_responders)}")

    return [_section_block("\n".join(lines))]


def _build_metadata_footer(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Build metadata footer context block."""
    incident_id = report.get("incidentId", "unknown")
    processing_duration = report.get("processingDuration", "unknown")

    text = (
        f"_Generated by CodeBlue AI | "
        f"Incident {incident_id} | "
        f"Processed in {processing_duration}_"
    )
    return [_context_block(text)]


# ---------------------------------------------------------------------------
# Plain-text fallback
# ---------------------------------------------------------------------------


def _build_plain_text_fallback(report: dict[str, Any]) -> str:
    """Build plain-text fallback summary for non-Block-Kit contexts."""
    severity = report.get("severity", "medium").upper()
    alert = report.get("alert", {})
    title = alert.get("title", "Incident Alert")

    blast_radius = report.get("blastRadius", {})
    blast_summary = blast_radius.get("summary", "Unknown") if blast_radius else "Unknown"

    hypotheses = report.get("hypotheses", [])
    confidence = report.get("confidence", 0.0)
    confidence_pct = int(confidence * 100)
    likely_cause = "No hypothesis generated"
    if hypotheses:
        likely_cause = hypotheses[0].get("title", "Unknown")

    escalation = report.get("escalation")
    escalation_line = "No escalation decision"
    if escalation:
        urgency = escalation.get("urgency", "business_hours")
        reason = escalation.get("reason", "")
        escalation_line = f"{urgency} — {reason}"

    incident_id = report.get("incidentId", "unknown")
    processing_duration = report.get("processingDuration", "unknown")

    return (
        f"[{severity}] {title}\n"
        f"Blast Radius: {blast_summary}\n"
        f"Likely Cause: {likely_cause} ({confidence_pct}% confidence)\n"
        f"Escalation: {escalation_line}\n"
        f"Generated by CodeBlue AI | {incident_id} | {processing_duration}"
    )


# ---------------------------------------------------------------------------
# Main formatting function
# ---------------------------------------------------------------------------


def format_incident_report(report: dict[str, Any]) -> dict[str, Any]:
    """Format an IncidentReport into a SlackMessage wrapped in SkillResponse.

    Returns SkillResponse dict with status, message, and data fields.
    """
    # Validate minimum required fields
    if not report.get("incidentId"):
        return _error("IncidentReport missing required field: 'incidentId'")
    if not report.get("alert"):
        return _error("IncidentReport missing required field: 'alert'")
    if not report.get("severity"):
        return _error("IncidentReport missing required field: 'severity'")

    # Get channel from environment
    channel = os.environ.get("CODEBLUE_SLACK_CHANNEL", "")
    if not channel:
        return _error(
            "No Slack channel configured. "
            "Set CODEBLUE_SLACK_CHANNEL environment variable."
        )

    # Assemble blocks in required order
    blocks: list[dict[str, Any]] = []

    # 1. Header
    blocks.extend(_build_header_section(report))

    # Divider after header
    blocks.append(_divider_block())

    # 2. Severity & Status
    blocks.extend(_build_severity_section(report))

    # 3. Blast Radius
    blast_blocks = _build_blast_radius_section(report)
    if blast_blocks:
        blocks.extend(blast_blocks)

    # Divider after blast radius
    if blast_blocks:
        blocks.append(_divider_block())

    # 4. Root Cause Hypothesis
    blocks.extend(_build_hypothesis_section(report))

    # 5. Evidence Links
    evidence_blocks = _build_evidence_section(report)
    if evidence_blocks:
        blocks.extend(evidence_blocks)

    # 6. Recommended Next Steps
    next_steps_blocks = _build_next_steps_section(report)
    if next_steps_blocks:
        blocks.extend(next_steps_blocks)

    # Divider before escalation
    escalation_blocks = _build_escalation_section(report)
    if escalation_blocks:
        blocks.append(_divider_block())
        blocks.extend(escalation_blocks)

    # Divider before footer
    blocks.append(_divider_block())

    # 8. Metadata Footer
    blocks.extend(_build_metadata_footer(report))

    # Build plain-text fallback
    fallback_text = _build_plain_text_fallback(report)

    # Build metadata
    metadata = {
        "incidentId": report.get("incidentId", "unknown"),
        "severity": report.get("severity", "medium"),
        "generatedBy": "codeblue-ai",
    }

    slack_message = {
        "channel": channel,
        "text": fallback_text,
        "blocks": blocks,
        "metadata": metadata,
    }

    return _success(slack_message)


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _success(data: dict[str, Any]) -> dict[str, Any]:
    """Wrap data in a success SkillResponse."""
    return {"status": "success", "message": "", "data": data}


def _error(message: str) -> dict[str, Any]:
    """Wrap message in an error SkillResponse."""
    return {"status": "error", "message": message, "data": None}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Read IncidentReport JSON from stdin, format, and write SlackMessage to stdout."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            result = _error("Empty input received on stdin")
        else:
            report = json.loads(raw)
            result = format_incident_report(report)
    except json.JSONDecodeError as e:
        result = _error(f"Invalid JSON input: {e}")
    except Exception as e:
        result = _error(f"Unexpected error during incident summary formatting: {e}")

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
