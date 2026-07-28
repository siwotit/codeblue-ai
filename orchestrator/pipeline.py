"""CodeBlue AI Pipeline — top-level entry point wiring all components end-to-end.

This module connects:
1. Alert ingestion (raw alarm payload → NormalizedAlert)
2. Workflow orchestrator (NormalizedAlert → IncidentReport)
3. Incident summary formatting (IncidentReport → SlackMessage)
4. Slack delivery (SlackMessage → posted to channel)

It also provides integration with the interactive triage mode so that
`start_investigation` can trigger the full orchestrator workflow.

Requirements: 10.5, 12.1, 12.2
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from skills.shared.models import IncidentReport, NormalizedAlert, Severity

from orchestrator.interactive_triage import (
    FieldAnnotation,
    TriageSession,
    start_investigation,
)
from orchestrator.skill_invoker_impl import SubprocessSkillInvoker
from orchestrator.slack_delivery import (
    DeliveryResult,
    SlackDeliveryService,
    SlackMCPClient,
)
from orchestrator.workflow_orchestrator import (
    SkillInvoker,
    WorkflowOrchestrator,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pipeline Result
# ---------------------------------------------------------------------------


@dataclass
class PipelineResult:
    """Result of a full pipeline execution.

    Attributes:
        success: Whether the pipeline completed successfully.
        incident_report: The generated IncidentReport (if successful).
        slack_message: The formatted Slack message (if formatting succeeded).
        delivery_result: The Slack delivery result (if delivery was attempted).
        error: Error message if the pipeline failed.
        processing_duration_ms: Total wall-clock time from start to finish.
    """

    success: bool
    incident_report: IncidentReport | None = None
    slack_message: dict[str, Any] | None = None
    delivery_result: DeliveryResult | None = None
    error: str | None = None
    processing_duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# Alert Ingestion Helper
# ---------------------------------------------------------------------------


async def ingest_raw_alarm(
    raw_payload: dict[str, Any],
    skill_invoker: SkillInvoker,
) -> NormalizedAlert | None:
    """Pass a raw alarm payload through the alert-ingestion skill.

    Invokes the alert-ingestion skill to normalize the raw alarm into
    a NormalizedAlert.

    Args:
        raw_payload: Raw alarm payload (must include 'source' and 'payload' keys).
        skill_invoker: The skill invoker to use.

    Returns:
        NormalizedAlert if ingestion succeeded, None otherwise.
    """
    result = await skill_invoker.invoke(
        skill_name="alert-ingestion",
        input_data=raw_payload,
        timeout=5.0,  # Alert ingestion should be fast
    )

    if not result.success or not result.data:
        logger.error(
            "Alert ingestion failed: %s",
            result.error or "no output",
        )
        return None

    try:
        # The alert-ingestion skill returns a SkillResponse with result field
        alert_data = result.data.get("result", result.data)
        return NormalizedAlert(**alert_data)
    except Exception as e:
        logger.error("Failed to parse NormalizedAlert from ingestion output: %s", e)
        return None


# ---------------------------------------------------------------------------
# Incident Summary Formatting Helper
# ---------------------------------------------------------------------------


async def format_incident_report(
    report: IncidentReport,
    skill_invoker: SkillInvoker,
) -> dict[str, Any] | None:
    """Format an IncidentReport into a Slack message via incident-summary skill.

    Args:
        report: The completed IncidentReport.
        skill_invoker: The skill invoker to use.

    Returns:
        Formatted Slack message dict (Block Kit), or None on failure.
    """
    report_data = report.model_dump(by_alias=True, mode="json")

    result = await skill_invoker.invoke(
        skill_name="incident-summary",
        input_data=report_data,
        timeout=10.0,
    )

    if not result.success or not result.data:
        logger.warning(
            "Incident summary formatting failed: %s",
            result.error or "no output",
        )
        # Fallback: produce a minimal Slack message
        return _fallback_slack_message(report)

    # Extract the Slack message from the skill output
    output = result.data
    if "result" in output and isinstance(output["result"], dict):
        return output["result"]
    return output


def _fallback_slack_message(report: IncidentReport) -> dict[str, Any]:
    """Produce a minimal Slack message when formatting skill fails.

    Args:
        report: The IncidentReport to summarize.

    Returns:
        Minimal Slack message payload.
    """
    severity_emoji = {
        "critical": "🔴",
        "high": "🟠",
        "medium": "🟡",
        "low": "🟢",
    }
    emoji = severity_emoji.get(report.severity.value, "⚪")

    text = (
        f"{emoji} *[{report.severity.value.upper()}] {report.alert.title}*\n"
        f"Incident: {report.incident_id}\n"
        f"Region: {report.alert.region}\n"
        f"Processing time: {report.processing_duration}\n"
        f"_Generated by CodeBlue AI (formatting degraded)_"
    )

    return {
        "text": text,
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            }
        ],
    }


# ---------------------------------------------------------------------------
# Full Pipeline Execution
# ---------------------------------------------------------------------------


async def run_pipeline(
    raw_payload: dict[str, Any],
    *,
    skill_invoker: SkillInvoker | None = None,
    slack_client: SlackMCPClient | None = None,
    slack_channel: str | None = None,
) -> PipelineResult:
    """Execute the full CodeBlue AI pipeline end-to-end.

    Flow:
        raw alarm → alert-ingestion → NormalizedAlert
        → WorkflowOrchestrator.handle_alert() → IncidentReport
        → incident-summary-format → SlackMessage
        → SlackDeliveryService → posted to channel

    Args:
        raw_payload: Raw alarm payload with 'source' and 'payload' keys.
        skill_invoker: SkillInvoker to use. Defaults to SubprocessSkillInvoker.
        slack_client: Slack MCP client for delivery. If None, delivery is skipped.
        slack_channel: Slack channel for delivery. Uses env default if not provided.

    Returns:
        PipelineResult with the outcome of each stage.

    Requirements: 10.5, 12.1, 12.2
    """
    start_time = time.monotonic()

    # Default to production subprocess invoker
    if skill_invoker is None:
        skill_invoker = SubprocessSkillInvoker()

    # Step 1: Alert Ingestion
    logger.info("Pipeline: Step 1 — Alert ingestion")
    alert = await ingest_raw_alarm(raw_payload, skill_invoker)
    if alert is None:
        return PipelineResult(
            success=False,
            error="Alert ingestion failed — could not normalize raw payload",
            processing_duration_ms=(time.monotonic() - start_time) * 1000,
        )

    # Step 2: Orchestrator workflow
    logger.info("Pipeline: Step 2 — Workflow orchestration (alert: %s)", alert.id)
    orchestrator = WorkflowOrchestrator(skill_invoker)
    report = await orchestrator.handle_alert(alert)

    # Step 3: Incident summary formatting
    logger.info("Pipeline: Step 3 — Formatting incident report")
    slack_message = await format_incident_report(report, skill_invoker)

    # Step 4: Slack delivery
    delivery_result: DeliveryResult | None = None
    if slack_client is not None and slack_message is not None:
        logger.info("Pipeline: Step 4 — Slack delivery")
        delivery_service = SlackDeliveryService(
            slack_client,
            channel=slack_channel,
        )
        delivery_result = await delivery_service.deliver(
            slack_message,
            incident_id=report.incident_id,
        )
    else:
        logger.info("Pipeline: Step 4 — Slack delivery skipped (no client configured)")

    processing_duration_ms = (time.monotonic() - start_time) * 1000

    return PipelineResult(
        success=True,
        incident_report=report,
        slack_message=slack_message,
        delivery_result=delivery_result,
        processing_duration_ms=processing_duration_ms,
    )


# ---------------------------------------------------------------------------
# Interactive Triage → Full Orchestrator Integration
# ---------------------------------------------------------------------------


async def triage_with_full_workflow(
    prompt: str,
    *,
    skill_invoker: SkillInvoker | None = None,
    slack_client: SlackMCPClient | None = None,
    slack_channel: str | None = None,
) -> tuple[TriageSession, PipelineResult]:
    """Start an interactive triage and run the full orchestrator workflow.

    Connects the interactive triage mode to the full pipeline:
    1. Synthesize NormalizedAlert from natural language
    2. Pass the alert through the full orchestrator workflow
    3. Optionally deliver to Slack

    Args:
        prompt: Natural language description of the incident.
        skill_invoker: SkillInvoker to use. Defaults to SubprocessSkillInvoker.
        slack_client: Slack MCP client. If None, delivery is skipped.
        slack_channel: Target Slack channel.

    Returns:
        Tuple of (TriageSession, PipelineResult).
    """
    if skill_invoker is None:
        skill_invoker = SubprocessSkillInvoker()

    # Step 1: Start interactive investigation (synthesizes NormalizedAlert)
    session, annotations, initial_skills = start_investigation(prompt)

    logger.info(
        "Triage session %s started — running full orchestrator workflow",
        session.session_id,
    )

    # Step 2: Run the full orchestrator workflow with the synthesized alert
    orchestrator = WorkflowOrchestrator(skill_invoker)
    report = await orchestrator.handle_alert(session.normalized_alert)

    # Step 3: Format and deliver
    slack_message = await format_incident_report(report, skill_invoker)

    delivery_result: DeliveryResult | None = None
    if slack_client is not None and slack_message is not None:
        delivery_service = SlackDeliveryService(
            slack_client,
            channel=slack_channel,
        )
        delivery_result = await delivery_service.deliver(
            slack_message,
            incident_id=report.incident_id,
        )

    processing_duration_ms = (time.monotonic() - session.started_at.timestamp()) * 1000

    pipeline_result = PipelineResult(
        success=True,
        incident_report=report,
        slack_message=slack_message,
        delivery_result=delivery_result,
        processing_duration_ms=processing_duration_ms,
    )

    return session, pipeline_result
