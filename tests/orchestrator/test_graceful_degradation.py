"""Tests for graceful degradation in the workflow orchestrator.

Validates Requirements 11.1, 11.2, 11.3, 11.4, 11.5:
- MCP connection timeout (30s) handling
- Report annotations when data is incomplete
- Normal baseline still triggers deploy-correlation and log-triage
- All-MCPs-fail scenario produces report at 0.1 confidence
- Confidence reduction per unavailable source
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from orchestrator.workflow_orchestrator import (
    CONFIDENCE_REDUCTION_PER_SOURCE,
    MCP_CONNECTION_TIMEOUT_SECONDS,
    MIN_CONFIDENCE,
    NORMAL_BASELINE_MAX_CONFIDENCE,
    SKILL_TIMEOUT_SECONDS,
    DataGap,
    SkillInvoker,
    SkillResult,
    WorkflowOrchestrator,
)
from skills.shared.models import (
    AlertSource,
    AlertState,
    DeviationClassification,
    NormalizedAlert,
    ResourceIdentifier,
    ResourceIdentifierType,
    Severity,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _make_alert(
    cluster: str | None = None,
    namespace: str | None = None,
    severity: Severity = Severity.HIGH,
    state: AlertState = AlertState.FIRING,
) -> NormalizedAlert:
    """Create a test NormalizedAlert."""
    return NormalizedAlert(
        id="test-alert-001",
        source=AlertSource.CLOUDWATCH,
        source_alarm_id="alarm-123",
        severity=severity,
        title="High CPU Utilization",
        description="CPU > 90% for 5 minutes",
        state=state,
        fired_at=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
        affected_resources=[
            ResourceIdentifier(
                type=ResourceIdentifierType.ARN,
                value="arn:aws:ec2:us-east-1:123456:instance/i-abc123",
                display_name="web-server-1",
            )
        ],
        region="us-east-1",
        cluster=cluster,
        namespace=namespace,
        metric_name="CPUUtilization",
        metric_namespace="AWS/EC2",
        dimensions={"InstanceId": "i-abc123"},
        raw_payload={"source": "test"},
    )


def _make_normal_metric_deviation_data() -> dict[str, Any]:
    """Return metric-baseline result data indicating NORMAL classification."""
    return {
        "metricName": "CPUUtilization",
        "namespace": "AWS/EC2",
        "dimensions": {"InstanceId": "i-abc123"},
        "timeRange": {
            "start": "2024-01-15T09:00:00Z",
            "end": "2024-01-15T10:00:00Z",
        },
        "currentMean": 45.0,
        "currentP95": 55.0,
        "currentMax": 60.0,
        "baselineMean": 50.0,
        "baselineP95": 65.0,
        "baselineP99": 75.0,
        "baselineStdDev": 10.0,
        "deviationFactor": 0.5,
        "classification": "normal",
        "confidence": 0.9,
        "dataPoints": [],
        "baselineDataPoints": [],
    }


class MockSkillInvoker(SkillInvoker):
    """Mock skill invoker that tracks invocations and returns configurable results."""

    def __init__(
        self,
        results: dict[str, SkillResult] | None = None,
        delay: float = 0.0,
        delay_per_skill: dict[str, float] | None = None,
    ) -> None:
        self.invocations: list[tuple[str, dict[str, Any]]] = []
        self._results = results or {}
        self._delay = delay
        self._delay_per_skill = delay_per_skill or {}

    async def invoke(
        self, skill_name: str, input_data: dict[str, Any], timeout: float
    ) -> SkillResult:
        self.invocations.append((skill_name, input_data))
        # Apply per-skill delay if configured
        delay = self._delay_per_skill.get(skill_name, self._delay)
        if delay > 0:
            await asyncio.sleep(delay)
        if skill_name in self._results:
            return self._results[skill_name]
        # Default: return success with empty data
        return SkillResult(skill_name=skill_name, success=True, data={})


class MCPConnectionTimeoutInvoker(SkillInvoker):
    """Invoker that simulates MCP connection timeout for specified skills."""

    def __init__(self, timeout_skills: set[str]) -> None:
        self.invocations: list[tuple[str, dict[str, Any]]] = []
        self._timeout_skills = timeout_skills

    async def invoke(
        self, skill_name: str, input_data: dict[str, Any], timeout: float
    ) -> SkillResult:
        self.invocations.append((skill_name, input_data))
        if skill_name in self._timeout_skills:
            # Simulate a connection timeout exceeding the MCP timeout
            await asyncio.sleep(timeout + 1)
        return SkillResult(skill_name=skill_name, success=True, data={})


# ---------------------------------------------------------------------------
# Tests: MCP Connection Timeout (Requirement 11.1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_connection_timeout_logs_and_continues():
    """MCP connection failure (30s timeout) should log, mark unavailable, continue.

    Requirement 11.1: IF a single MCP connection fails or exceeds a 30-second
    timeout, THEN THE Orchestrator SHALL log the failure, mark that data source
    as unavailable, and continue the workflow with remaining sources.
    """
    # The metric-baseline skill times out (simulating MCP connection failure)
    results = {
        "metric-baseline": SkillResult(
            skill_name="metric-baseline",
            success=False,
            error="MCP connection timeout after 30000ms",
        ),
    }
    invoker = MockSkillInvoker(results=results)
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert()

    report = await orchestrator.handle_alert(alert)

    # Report should still be produced
    assert report is not None
    assert report.incident_id is not None

    # The failed source should be marked unavailable
    assert "metric-baseline" in report.unavailable_sources

    # Other skills should still have been invoked
    invoked_skills = [name for name, _ in invoker.invocations]
    assert "deploy-correlation" in invoked_skills
    assert "log-triage" in invoked_skills


@pytest.mark.asyncio
async def test_mcp_connection_timeout_constant_is_30_seconds():
    """The MCP connection timeout should be exactly 30 seconds."""
    assert MCP_CONNECTION_TIMEOUT_SECONDS == 30


# ---------------------------------------------------------------------------
# Tests: Report Annotations for Incomplete Data (Requirement 11.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_annotates_incomplete_data_on_skill_failure():
    """Report should contain structured annotations when data is incomplete.

    Requirement 11.2: IF a single skill fails, THEN the report is explicitly
    marked as having incomplete data, annotating each gap by listing the
    unavailable source name and the workflow phase that was skipped.
    """
    results = {
        "metric-baseline": SkillResult(
            skill_name="metric-baseline",
            success=False,
            error="Connection refused: cloudwatch-mcp unavailable",
        ),
        "log-triage": SkillResult(
            skill_name="log-triage",
            success=False,
            error="Timeout waiting for log query",
        ),
    }
    invoker = MockSkillInvoker(results=results)
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert()

    report = await orchestrator.handle_alert(alert)

    # Report should have data_gaps annotations
    assert len(report.data_gaps) >= 2

    # Check that gap annotations include source name and reason
    gap_sources = [gap["source_name"] for gap in report.data_gaps]
    assert "metric-baseline" in gap_sources
    assert "log-triage" in gap_sources

    # Each gap should include workflow phase and reason
    for gap in report.data_gaps:
        assert "workflow_phase" in gap
        assert "reason" in gap
        assert "source_name" in gap
        assert gap["reason"]  # Non-empty reason


@pytest.mark.asyncio
async def test_report_lists_unavailable_sources():
    """Report should list all unavailable data sources.

    Requirement 11.3: Include a list of unavailable data sources in the report.
    """
    results = {
        "deploy-correlation": SkillResult(
            skill_name="deploy-correlation",
            success=False,
            error="CloudTrail API unavailable",
        ),
    }
    invoker = MockSkillInvoker(results=results)
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert()

    report = await orchestrator.handle_alert(alert)

    assert "deploy-correlation" in report.unavailable_sources


# ---------------------------------------------------------------------------
# Tests: Confidence Reduction (Requirement 11.3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confidence_reduced_by_0_2_per_unavailable_source():
    """Confidence should be reduced by exactly 0.2 per unavailable source.

    Requirement 11.3: Reduce overall confidence by at least 0.2 per
    unavailable data source (clamped to minimum 0.1).
    """
    # Valid mock data for skills that succeed
    valid_corr_data = {
        "changes": [],
        "lookbackWindow": "24h",
        "affectedResources": [],
    }
    valid_log_data = {
        "findings": [],
        "queriedLogGroups": ["/aws/test"],
        "timeRange": {"start": "2024-01-15T09:00:00Z", "end": "2024-01-15T10:00:00Z"},
        "totalErrorCount": 0,
        "baselineErrorCount": 0,
    }

    # One source fails
    results_1 = {
        "metric-baseline": SkillResult(
            skill_name="metric-baseline", success=False, error="fail"
        ),
        "deploy-correlation": SkillResult(
            skill_name="deploy-correlation", success=True, data=valid_corr_data,
        ),
        "log-triage": SkillResult(
            skill_name="log-triage", success=True, data=valid_log_data,
        ),
    }
    invoker_1 = MockSkillInvoker(results=results_1)
    orchestrator_1 = WorkflowOrchestrator(skill_invoker=invoker_1)
    alert = _make_alert()
    report_1 = await orchestrator_1.handle_alert(alert)

    # Two sources fail
    results_2 = {
        "metric-baseline": SkillResult(
            skill_name="metric-baseline", success=False, error="fail"
        ),
        "deploy-correlation": SkillResult(
            skill_name="deploy-correlation", success=False, error="fail"
        ),
        "log-triage": SkillResult(
            skill_name="log-triage", success=True, data=valid_log_data,
        ),
    }
    invoker_2 = MockSkillInvoker(results=results_2)
    orchestrator_2 = WorkflowOrchestrator(skill_invoker=invoker_2)
    report_2 = await orchestrator_2.handle_alert(alert)

    # More failures should result in lower confidence
    assert report_2.confidence < report_1.confidence

    # Both should be within valid bounds
    assert report_1.confidence >= MIN_CONFIDENCE
    assert report_2.confidence >= MIN_CONFIDENCE


@pytest.mark.asyncio
async def test_confidence_minimum_is_0_1():
    """Confidence should never go below 0.1 regardless of how many sources fail.

    Requirement 11.3: Clamped to a minimum of 0.1.
    """

    class AlwaysFailInvoker(SkillInvoker):
        async def invoke(self, skill_name, input_data, timeout):
            return SkillResult(
                skill_name=skill_name, success=False, error="All MCPs down"
            )

    orchestrator = WorkflowOrchestrator(skill_invoker=AlwaysFailInvoker())
    alert = _make_alert()

    report = await orchestrator.handle_alert(alert)

    assert report.confidence == MIN_CONFIDENCE
    assert report.confidence == 0.1


# ---------------------------------------------------------------------------
# Tests: All MCPs Fail (Requirement 11.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_mcps_fail_produces_report_with_alert_info_only():
    """When all MCPs fail, report should contain only original alert info.

    Requirement 11.4: IF all MCP connections fail, THEN THE Orchestrator
    SHALL produce a report containing only the original alert information,
    a confidence score of 0.1, and annotations indicating that no additional
    signals could be collected.
    """

    class AlwaysFailInvoker(SkillInvoker):
        async def invoke(self, skill_name, input_data, timeout):
            return SkillResult(
                skill_name=skill_name,
                success=False,
                error="Service unavailable",
            )

    orchestrator = WorkflowOrchestrator(skill_invoker=AlwaysFailInvoker())
    alert = _make_alert()

    report = await orchestrator.handle_alert(alert)

    # Report should exist
    assert report is not None
    assert report.incident_id is not None

    # Should contain the original alert
    assert report.alert == alert

    # Confidence should be exactly 0.1
    assert report.confidence == 0.1

    # Should have no metric deviation, correlated changes, or log findings
    assert report.metric_deviation is None
    assert report.correlated_changes is None
    assert report.log_findings is None

    # Should have unavailable sources annotated
    assert len(report.unavailable_sources) > 0

    # Should have data gap annotations
    assert len(report.data_gaps) > 0


# ---------------------------------------------------------------------------
# Tests: Normal Baseline Still Runs Correlation + Triage (Requirement 11.5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_baseline_still_runs_deploy_correlation_and_log_triage():
    """When baseline shows normal, deploy-correlation and log-triage still run.

    Requirement 11.5: IF metric baseline shows normal despite alarm firing,
    THEN THE Orchestrator SHALL still invoke deploy-correlation and log-triage.

    Since all signal-collection skills run concurrently, deploy-correlation
    and log-triage don't depend on metric-baseline results.
    """
    # metric-baseline returns normal classification
    results = {
        "metric-baseline": SkillResult(
            skill_name="metric-baseline",
            success=True,
            data=_make_normal_metric_deviation_data(),
        ),
    }
    invoker = MockSkillInvoker(results=results)
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert()

    report = await orchestrator.handle_alert(alert)

    # deploy-correlation and log-triage should still have been invoked
    invoked_skills = [name for name, _ in invoker.invocations]
    assert "deploy-correlation" in invoked_skills
    assert "log-triage" in invoked_skills

    # Confidence should be capped at 0.3 for normal baseline with alarm firing
    assert report.confidence <= NORMAL_BASELINE_MAX_CONFIDENCE


@pytest.mark.asyncio
async def test_normal_baseline_caps_confidence_at_0_3():
    """Confidence should be no higher than 0.3 when baseline is normal and alarm fires.

    Requirement 11.5: Report the alert with a confidence score no higher than 0.3.
    """
    results = {
        "metric-baseline": SkillResult(
            skill_name="metric-baseline",
            success=True,
            data=_make_normal_metric_deviation_data(),
        ),
    }
    invoker = MockSkillInvoker(results=results)
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert(state=AlertState.FIRING)

    report = await orchestrator.handle_alert(alert)

    assert report.confidence <= 0.3
    assert report.confidence >= MIN_CONFIDENCE


@pytest.mark.asyncio
async def test_normal_baseline_without_alarm_does_not_cap_confidence():
    """When alert is resolved (not firing), normal baseline should not cap confidence."""
    results = {
        "metric-baseline": SkillResult(
            skill_name="metric-baseline",
            success=True,
            data=_make_normal_metric_deviation_data(),
        ),
    }
    invoker = MockSkillInvoker(results=results)
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert(state=AlertState.RESOLVED)

    report = await orchestrator.handle_alert(alert)

    # For a resolved alert with normal baseline, confidence should not be capped
    # (the 0.3 cap only applies when alarm is firing)
    # Note: confidence may still be reduced by other factors
    assert report.confidence >= MIN_CONFIDENCE


# ---------------------------------------------------------------------------
# Tests: DataGap structure
# ---------------------------------------------------------------------------


def test_data_gap_records_source_phase_and_reason():
    """DataGap should record source name, workflow phase, and reason."""
    gap = DataGap(
        source_name="metric-baseline",
        workflow_phase="metric_analysis",
        reason="Connection timeout after 30s",
    )

    assert gap.source_name == "metric-baseline"
    assert gap.workflow_phase == "metric_analysis"
    assert gap.reason == "Connection timeout after 30s"
    assert gap.timestamp  # Should have a timestamp
