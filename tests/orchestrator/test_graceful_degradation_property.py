"""Property-based test for graceful degradation on partial failure.

**Property 17: Graceful degradation on partial failure**
**Validates: Requirements 11.1, 11.2, 11.3, 12.5**

For any combination of MCP/skill failures (up to N-1 of N), verify:
1. A report is still produced (never raises/crashes)
2. Confidence is reduced (more failures → lower confidence)
3. Gap annotations exist for failed skills
4. confidence >= 0.1 always (MIN_CONFIDENCE)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from orchestrator.workflow_orchestrator import (
    CONFIDENCE_REDUCTION_PER_SOURCE,
    MIN_CONFIDENCE,
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
# Constants
# ---------------------------------------------------------------------------

# All signal skills that the orchestrator dispatches for a non-K8s alert
CORE_SIGNAL_SKILLS = frozenset({
    "metric-baseline",
    "deploy-correlation",
    "log-triage",
})

# K8s skills only dispatched when cluster is non-empty
K8S_SKILLS = frozenset({
    "k8s-cluster-health",
    "pod-failure-triage",
    "node-condition-check",
    "eks-addon-status",
})

# All possible signal skills for a K8s-scoped alert
ALL_SIGNAL_SKILLS = CORE_SIGNAL_SKILLS | K8S_SKILLS


# ---------------------------------------------------------------------------
# Test fixtures and helpers
# ---------------------------------------------------------------------------


def _make_alert(with_cluster: bool = False) -> NormalizedAlert:
    """Create a test NormalizedAlert."""
    return NormalizedAlert(
        id="prop-test-alert-001",
        source=AlertSource.CLOUDWATCH,
        source_alarm_id="alarm-prop-001",
        severity=Severity.HIGH,
        title="Property Test Alert",
        description="Generated for property-based testing",
        state=AlertState.FIRING,
        fired_at=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
        affected_resources=[
            ResourceIdentifier(
                type=ResourceIdentifierType.ARN,
                value="arn:aws:ec2:us-east-1:123456:instance/i-prop123",
                display_name="prop-test-service",
            )
        ],
        region="us-east-1",
        cluster="prod-cluster" if with_cluster else None,
        namespace="default" if with_cluster else None,
        metric_name="CPUUtilization",
        metric_namespace="AWS/EC2",
        dimensions={"InstanceId": "i-prop123"},
        raw_payload={"source": "property-test"},
    )


class PartialFailureInvoker(SkillInvoker):
    """Skill invoker that fails for a specific set of skills."""

    def __init__(self, failing_skills: set[str]) -> None:
        self.failing_skills = failing_skills
        self.invocations: list[str] = []

    async def invoke(
        self, skill_name: str, input_data: dict[str, Any], timeout: float
    ) -> SkillResult:
        self.invocations.append(skill_name)
        if skill_name in self.failing_skills:
            return SkillResult(
                skill_name=skill_name,
                success=False,
                error=f"Simulated failure for {skill_name}",
            )
        # Return minimal valid data for successful skills
        return SkillResult(
            skill_name=skill_name,
            success=True,
            data={},
        )


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Strategy: generate a non-empty subset of core signal skills to fail
# (up to N-1 so at least one can succeed, OR all can fail for the all-fail case)
failing_core_skills_strategy = st.frozensets(
    st.sampled_from(sorted(CORE_SIGNAL_SKILLS)),
    min_size=1,
    max_size=len(CORE_SIGNAL_SKILLS),
)

# Strategy: generate any subset of all skills (including K8s skills) to fail
failing_all_skills_strategy = st.frozensets(
    st.sampled_from(sorted(ALL_SIGNAL_SKILLS)),
    min_size=1,
    max_size=len(ALL_SIGNAL_SKILLS),
)


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@settings(max_examples=50, deadline=10000)
@given(failing_skills=failing_core_skills_strategy)
async def test_report_always_produced_on_partial_failure(
    failing_skills: frozenset[str],
) -> None:
    """Property: For any combination of skill failures (up to N-1 of N),
    a report is always produced without raising an exception.

    **Validates: Requirements 11.1, 11.2, 11.3, 12.5**
    """
    alert = _make_alert(with_cluster=False)
    invoker = PartialFailureInvoker(failing_skills=set(failing_skills))
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)

    # The orchestrator must never crash — it always produces a report
    report = await orchestrator.handle_alert(alert)

    assert report is not None
    assert report.incident_id is not None
    assert report.alert == alert


@pytest.mark.asyncio
@settings(max_examples=50, deadline=10000)
@given(failing_skills=failing_core_skills_strategy)
async def test_confidence_reduced_with_more_failures(
    failing_skills: frozenset[str],
) -> None:
    """Property: More skill failures always result in equal or lower confidence
    than fewer failures.

    **Validates: Requirements 11.3, 12.5**
    """
    alert = _make_alert(with_cluster=False)

    # Run with the generated failing skills
    invoker_partial = PartialFailureInvoker(failing_skills=set(failing_skills))
    orchestrator_partial = WorkflowOrchestrator(skill_invoker=invoker_partial)
    report_partial = await orchestrator_partial.handle_alert(alert)

    # Run with zero failures as baseline
    invoker_none = PartialFailureInvoker(failing_skills=set())
    orchestrator_none = WorkflowOrchestrator(skill_invoker=invoker_none)
    report_none = await orchestrator_none.handle_alert(alert)

    # Confidence with failures must be <= confidence with no failures
    assert report_partial.confidence <= report_none.confidence


@pytest.mark.asyncio
@settings(max_examples=50, deadline=10000)
@given(failing_skills=failing_core_skills_strategy)
async def test_confidence_never_below_minimum(
    failing_skills: frozenset[str],
) -> None:
    """Property: Confidence is always >= MIN_CONFIDENCE (0.1) regardless
    of how many skills fail.

    **Validates: Requirements 11.3, 12.5**
    """
    alert = _make_alert(with_cluster=False)
    invoker = PartialFailureInvoker(failing_skills=set(failing_skills))
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)

    report = await orchestrator.handle_alert(alert)

    assert report.confidence >= MIN_CONFIDENCE
    assert report.confidence >= 0.1


@pytest.mark.asyncio
@settings(max_examples=50, deadline=10000)
@given(failing_skills=failing_core_skills_strategy)
async def test_gap_annotations_exist_for_failed_skills(
    failing_skills: frozenset[str],
) -> None:
    """Property: For every failed skill, a corresponding data gap annotation
    exists in the report listing the source name and workflow phase.

    **Validates: Requirements 11.2, 12.5**
    """
    alert = _make_alert(with_cluster=False)
    invoker = PartialFailureInvoker(failing_skills=set(failing_skills))
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)

    report = await orchestrator.handle_alert(alert)

    # Every failed skill that was actually invoked should have a gap annotation
    invoked_and_failed = set(invoker.invocations) & set(failing_skills)

    gap_sources = {gap["source_name"] for gap in report.data_gaps}
    for skill in invoked_and_failed:
        assert skill in gap_sources, (
            f"Failed skill '{skill}' missing from data_gaps. "
            f"Gaps found: {gap_sources}"
        )

    # Every gap annotation should have required fields
    for gap in report.data_gaps:
        assert "source_name" in gap
        assert "workflow_phase" in gap
        assert "reason" in gap
        assert gap["source_name"]  # non-empty
        assert gap["workflow_phase"]  # non-empty
        assert gap["reason"]  # non-empty


@pytest.mark.asyncio
@settings(max_examples=50, deadline=10000)
@given(failing_skills=failing_all_skills_strategy)
async def test_k8s_alert_graceful_degradation(
    failing_skills: frozenset[str],
) -> None:
    """Property: For K8s-scoped alerts, any combination of skill failures
    (core + K8s) still produces a valid report with correct gap annotations.

    **Validates: Requirements 11.1, 11.2, 11.3, 12.5**
    """
    alert = _make_alert(with_cluster=True)
    invoker = PartialFailureInvoker(failing_skills=set(failing_skills))
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)

    report = await orchestrator.handle_alert(alert)

    # Report is always produced
    assert report is not None
    assert report.incident_id is not None

    # Confidence respects minimum
    assert report.confidence >= MIN_CONFIDENCE

    # Failed skills that were invoked should appear in unavailable_sources
    invoked_and_failed = set(invoker.invocations) & set(failing_skills)
    for skill in invoked_and_failed:
        assert skill in report.unavailable_sources, (
            f"Failed skill '{skill}' not in unavailable_sources"
        )


@pytest.mark.asyncio
@settings(max_examples=30, deadline=10000)
@given(
    failing_set_a=st.frozensets(
        st.sampled_from(sorted(CORE_SIGNAL_SKILLS)),
        min_size=1,
        max_size=len(CORE_SIGNAL_SKILLS) - 1,
    ),
    failing_set_b=st.frozensets(
        st.sampled_from(sorted(CORE_SIGNAL_SKILLS)),
        min_size=1,
        max_size=len(CORE_SIGNAL_SKILLS) - 1,
    ),
)
async def test_more_failures_lower_or_equal_confidence(
    failing_set_a: frozenset[str],
    failing_set_b: frozenset[str],
) -> None:
    """Property: If set A is a strict subset of set B (A ⊂ B),
    then confidence(B) <= confidence(A).

    **Validates: Requirements 11.3, 12.5**
    """
    # Only test when A is a strict subset of B
    assume(failing_set_a < failing_set_b)

    alert = _make_alert(with_cluster=False)

    invoker_a = PartialFailureInvoker(failing_skills=set(failing_set_a))
    orchestrator_a = WorkflowOrchestrator(skill_invoker=invoker_a)
    report_a = await orchestrator_a.handle_alert(alert)

    invoker_b = PartialFailureInvoker(failing_skills=set(failing_set_b))
    orchestrator_b = WorkflowOrchestrator(skill_invoker=invoker_b)
    report_b = await orchestrator_b.handle_alert(alert)

    assert report_b.confidence <= report_a.confidence
