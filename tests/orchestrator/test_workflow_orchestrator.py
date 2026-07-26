"""Tests for the workflow orchestrator core logic.

Validates:
- Skill determination based on alert context (EKS vs non-EKS)
- Concurrent execution of signal collection skills
- Workflow phase tracking
- Confidence reduction for unavailable sources
- Report assembly
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from orchestrator.workflow_orchestrator import (
    CUMULATIVE_TIMEOUT_SECONDS,
    SKILL_TIMEOUT_SECONDS,
    SkillInvoker,
    SkillResult,
    WorkflowOrchestrator,
)
from skills.shared.models import (
    AlertSource,
    AlertState,
    NormalizedAlert,
    ResourceIdentifier,
    ResourceIdentifierType,
    Severity,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class MockSkillInvoker(SkillInvoker):
    """Mock skill invoker that tracks invocations and returns configurable results."""

    def __init__(
        self,
        results: dict[str, SkillResult] | None = None,
        delay: float = 0.0,
    ) -> None:
        self.invocations: list[tuple[str, dict[str, Any]]] = []
        self._results = results or {}
        self._delay = delay

    async def invoke(
        self, skill_name: str, input_data: dict[str, Any], timeout: float
    ) -> SkillResult:
        self.invocations.append((skill_name, input_data))
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        if skill_name in self._results:
            return self._results[skill_name]
        # Default: return success with empty data
        return SkillResult(skill_name=skill_name, success=True, data={})


def _make_alert(
    cluster: str | None = None,
    namespace: str | None = None,
    severity: Severity = Severity.HIGH,
) -> NormalizedAlert:
    """Create a test NormalizedAlert."""
    return NormalizedAlert(
        id="test-alert-001",
        source=AlertSource.CLOUDWATCH,
        source_alarm_id="alarm-123",
        severity=severity,
        title="High CPU Utilization",
        description="CPU > 90% for 5 minutes",
        state=AlertState.FIRING,
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_alert_non_eks_skips_k8s_skills():
    """Non-EKS alerts should NOT invoke K8s health skills (Req 3.7)."""
    invoker = MockSkillInvoker()
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert(cluster=None)

    report = await orchestrator.handle_alert(alert)

    invoked_skills = [name for name, _ in invoker.invocations]
    assert "k8s-cluster-health" not in invoked_skills
    assert "pod-failure-triage" not in invoked_skills
    assert "node-condition-check" not in invoked_skills
    assert "eks-addon-status" not in invoked_skills

    # Should still run metric-baseline, deploy-correlation, log-triage
    assert "metric-baseline" in invoked_skills
    assert "deploy-correlation" in invoked_skills
    assert "log-triage" in invoked_skills


@pytest.mark.asyncio
async def test_handle_alert_eks_includes_k8s_skills():
    """EKS alerts (non-empty cluster) SHOULD invoke K8s health skills."""
    invoker = MockSkillInvoker()
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert(cluster="prod-cluster", namespace="payments")

    report = await orchestrator.handle_alert(alert)

    invoked_skills = [name for name, _ in invoker.invocations]
    assert "k8s-cluster-health" in invoked_skills
    assert "pod-failure-triage" in invoked_skills
    assert "node-condition-check" in invoked_skills
    assert "eks-addon-status" in invoked_skills


@pytest.mark.asyncio
async def test_handle_alert_produces_valid_report():
    """handle_alert should produce a valid IncidentReport."""
    invoker = MockSkillInvoker()
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert()

    report = await orchestrator.handle_alert(alert)

    assert report.incident_id is not None
    assert report.alert == alert
    assert report.severity == Severity.HIGH
    assert report.generated_at is not None
    assert report.processing_duration.endswith("ms")
    assert report.blast_radius is not None
    assert report.confidence >= 0.1
    assert report.confidence <= 1.0


@pytest.mark.asyncio
async def test_handle_alert_runs_hypothesis_engine():
    """hypothesis-engine should be invoked after signal collection."""
    invoker = MockSkillInvoker()
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert()

    await orchestrator.handle_alert(alert)

    invoked_skills = [name for name, _ in invoker.invocations]
    assert "hypothesis-engine" in invoked_skills


@pytest.mark.asyncio
async def test_handle_alert_runs_escalation_and_provenance():
    """escalation-decision and evidence-provenance should run after hypothesis."""
    invoker = MockSkillInvoker()
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert()

    await orchestrator.handle_alert(alert)

    invoked_skills = [name for name, _ in invoker.invocations]
    assert "escalation-decision" in invoked_skills
    assert "evidence-provenance" in invoked_skills


@pytest.mark.asyncio
async def test_skill_execution_order():
    """Skills should execute in dependency order:
    signal-collection → hypothesis → escalation+provenance.
    """
    invocation_order: list[str] = []

    class OrderTrackingInvoker(SkillInvoker):
        async def invoke(self, skill_name, input_data, timeout):
            invocation_order.append(skill_name)
            return SkillResult(skill_name=skill_name, success=True, data={})

    orchestrator = WorkflowOrchestrator(skill_invoker=OrderTrackingInvoker())
    alert = _make_alert()

    await orchestrator.handle_alert(alert)

    # Find indices
    signal_skills = {"metric-baseline", "deploy-correlation", "log-triage"}
    hypothesis_idx = invocation_order.index("hypothesis-engine")
    final_skills = {"escalation-decision", "evidence-provenance"}

    # All signal skills should come before hypothesis
    for skill in signal_skills:
        if skill in invocation_order:
            assert invocation_order.index(skill) < hypothesis_idx

    # Escalation and provenance should come after hypothesis
    for skill in final_skills:
        if skill in invocation_order:
            assert invocation_order.index(skill) > hypothesis_idx


@pytest.mark.asyncio
async def test_skill_failure_marks_source_unavailable():
    """When a skill fails, its source should be marked unavailable."""
    results = {
        "metric-baseline": SkillResult(
            skill_name="metric-baseline",
            success=False,
            error="Connection timeout",
        ),
    }
    invoker = MockSkillInvoker(results=results)
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert()

    report = await orchestrator.handle_alert(alert)

    assert report.metric_deviation is None
    assert report.confidence < 0.8  # Reduced due to unavailable source


@pytest.mark.asyncio
async def test_confidence_reduced_per_unavailable_source():
    """Confidence should be reduced by 0.2 per unavailable source (Req 11.3)."""
    # All signal skills fail
    results = {
        "metric-baseline": SkillResult(
            skill_name="metric-baseline", success=False, error="fail"
        ),
        "deploy-correlation": SkillResult(
            skill_name="deploy-correlation", success=False, error="fail"
        ),
        "log-triage": SkillResult(
            skill_name="log-triage", success=False, error="fail"
        ),
    }
    invoker = MockSkillInvoker(results=results)
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert()

    report = await orchestrator.handle_alert(alert)

    # With 3 unavailable sources, confidence should be heavily reduced
    # but clamped to minimum 0.1
    assert report.confidence >= 0.1
    assert report.confidence <= 0.4


@pytest.mark.asyncio
async def test_workflow_completes_with_all_skills_failing():
    """Workflow should still produce a report when all skills fail (Req 11.4)."""

    class AlwaysFailInvoker(SkillInvoker):
        async def invoke(self, skill_name, input_data, timeout):
            return SkillResult(
                skill_name=skill_name, success=False, error="Service unavailable"
            )

    orchestrator = WorkflowOrchestrator(skill_invoker=AlwaysFailInvoker())
    alert = _make_alert()

    report = await orchestrator.handle_alert(alert)

    # Report should still be produced with reduced confidence
    assert report is not None
    assert report.incident_id is not None
    # 3 signal sources unavailable: 0.8 - 3*0.2 = 0.2
    # Plus hypothesis/escalation/provenance also fail = more reduction
    # Clamped to minimum 0.1
    assert report.confidence >= 0.1
    assert report.confidence <= 0.3
    assert report.alert == alert


@pytest.mark.asyncio
async def test_blast_radius_computed_from_alert():
    """Blast radius should include data from the alert."""
    invoker = MockSkillInvoker()
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert()

    report = await orchestrator.handle_alert(alert)

    assert report.blast_radius.region == "us-east-1"
    assert len(report.blast_radius.affected_services) > 0
