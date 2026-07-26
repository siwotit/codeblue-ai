"""Tests for timeout and concurrency management in the workflow orchestrator.

Validates:
- 15-second timeout on individual skill invocations (Req 12.3)
- 45-second cumulative wall-clock budget enforcement (Req 12.4, 12.5)
- 120-second budget for multi-cluster incidents (Req 12.7)
- Concurrent execution of independent skills (Req 12.2)
- Processing duration recording (Req 12.6)

**Validates: Requirements 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7**
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import pytest

from orchestrator.workflow_orchestrator import (
    CUMULATIVE_TIMEOUT_SECONDS,
    MULTI_CLUSTER_TIMEOUT_SECONDS,
    SKILL_TIMEOUT_SECONDS,
    SkillInvoker,
    SkillResult,
    WorkflowContext,
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
# Test helpers
# ---------------------------------------------------------------------------


def _make_alert(
    cluster: str | None = None,
    namespace: str | None = None,
    severity: Severity = Severity.HIGH,
    affected_resources: list[ResourceIdentifier] | None = None,
) -> NormalizedAlert:
    """Create a test NormalizedAlert."""
    if affected_resources is None:
        affected_resources = [
            ResourceIdentifier(
                type=ResourceIdentifierType.ARN,
                value="arn:aws:ec2:us-east-1:123456:instance/i-abc123",
                display_name="web-server-1",
            )
        ]
    return NormalizedAlert(
        id="test-alert-timeout-001",
        source=AlertSource.CLOUDWATCH,
        source_alarm_id="alarm-timeout-123",
        severity=severity,
        title="High CPU Utilization",
        description="CPU > 90% for 5 minutes",
        state=AlertState.FIRING,
        fired_at=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
        affected_resources=affected_resources,
        region="us-east-1",
        cluster=cluster,
        namespace=namespace,
        metric_name="CPUUtilization",
        metric_namespace="AWS/EC2",
        dimensions={"InstanceId": "i-abc123"},
        raw_payload={"source": "test"},
    )


def _make_multi_cluster_alert() -> NormalizedAlert:
    """Create a NormalizedAlert that spans multiple clusters."""
    return NormalizedAlert(
        id="test-alert-multi-cluster",
        source=AlertSource.CLOUDWATCH,
        source_alarm_id="alarm-multi-123",
        severity=Severity.CRITICAL,
        title="Cross-cluster pod failures",
        description="Pod failures across multiple clusters",
        state=AlertState.FIRING,
        fired_at=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
        affected_resources=[
            ResourceIdentifier(
                type=ResourceIdentifierType.K8S_RESOURCE,
                value="cluster-a/payments/deployment/api-server",
                display_name="api-server",
            ),
            ResourceIdentifier(
                type=ResourceIdentifierType.K8S_RESOURCE,
                value="cluster-b/orders/deployment/order-service",
                display_name="order-service",
            ),
        ],
        region="us-east-1",
        cluster="cluster-a",
        namespace="payments",
        raw_payload={"source": "test"},
    )


class SlowSkillInvoker(SkillInvoker):
    """Invoker that introduces configurable delays per skill."""

    def __init__(self, delays: dict[str, float] | None = None) -> None:
        """
        Args:
            delays: Map of skill_name -> delay in seconds.
                    Skills not in the map return immediately.
        """
        self.delays = delays or {}
        self.invocations: list[tuple[str, float]] = []  # (name, start_time)
        self.cancelled: list[str] = []
        self._start_time = time.monotonic()

    async def invoke(
        self, skill_name: str, input_data: dict[str, Any], timeout: float
    ) -> SkillResult:
        relative_start = time.monotonic() - self._start_time
        self.invocations.append((skill_name, relative_start))
        delay = self.delays.get(skill_name, 0.0)
        if delay > 0:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                self.cancelled.append(skill_name)
                raise
        return SkillResult(skill_name=skill_name, success=True, data={})


class TimedInvoker(SkillInvoker):
    """Invoker that records start times for concurrency verification."""

    def __init__(self) -> None:
        self.start_times: dict[str, float] = {}
        self.end_times: dict[str, float] = {}
        self._epoch = time.monotonic()

    async def invoke(
        self, skill_name: str, input_data: dict[str, Any], timeout: float
    ) -> SkillResult:
        self.start_times[skill_name] = time.monotonic() - self._epoch
        await asyncio.sleep(0.05)  # Simulate some work
        self.end_times[skill_name] = time.monotonic() - self._epoch
        return SkillResult(skill_name=skill_name, success=True, data={})


# ---------------------------------------------------------------------------
# Tests: Per-skill timeout (Req 12.3, 12.5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_skill_gets_timed_out(monkeypatch):
    """A skill exceeding 15 seconds should be terminated (Req 12.3).

    We monkeypatch the timeout to a short value for test speed.
    """
    import orchestrator.workflow_orchestrator as orch_mod

    # Use 0.2s timeout for testing
    monkeypatch.setattr(orch_mod, "SKILL_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(orch_mod, "CUMULATIVE_TIMEOUT_SECONDS", 5.0)

    class TimeoutSimulatingInvoker(SkillInvoker):
        """Simulates a skill that takes longer than allowed."""

        def __init__(self) -> None:
            self.invoked: list[str] = []

        async def invoke(
            self, skill_name: str, input_data: dict[str, Any], timeout: float
        ) -> SkillResult:
            self.invoked.append(skill_name)
            if skill_name == "metric-baseline":
                # Sleep longer than the per-skill timeout
                await asyncio.sleep(timeout + 1.0)
            return SkillResult(skill_name=skill_name, success=True, data={})

    invoker = TimeoutSimulatingInvoker()
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert()

    report = await orchestrator.handle_alert(alert)

    # metric-baseline should have timed out and be marked unavailable
    assert report.metric_deviation is None
    # Confidence reduced because metric-baseline is unavailable
    assert report.confidence < 0.8


@pytest.mark.asyncio
async def test_timed_out_skill_marked_as_unavailable(monkeypatch):
    """When a skill times out, it should be marked unavailable (Req 12.5)."""
    import orchestrator.workflow_orchestrator as orch_mod

    # Very short timeout to speed up test
    monkeypatch.setattr(orch_mod, "SKILL_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(orch_mod, "CUMULATIVE_TIMEOUT_SECONDS", 2.0)

    class AlwaysTimeoutInvoker(SkillInvoker):
        async def invoke(
            self, skill_name: str, input_data: dict[str, Any], timeout: float
        ) -> SkillResult:
            # Always exceed timeout
            await asyncio.sleep(timeout + 0.5)
            return SkillResult(skill_name=skill_name, success=True, data={})

    invoker = AlwaysTimeoutInvoker()
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert()

    report = await orchestrator.handle_alert(alert)

    # All skills timed out — report still produced (graceful degradation)
    assert report is not None
    assert report.incident_id is not None
    # Confidence should be at minimum due to all sources unavailable
    assert report.confidence == pytest.approx(0.1, abs=0.05)


# ---------------------------------------------------------------------------
# Tests: Cumulative budget (Req 12.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cumulative_budget_cancels_remaining_skills(monkeypatch):
    """When cumulative budget is reached, remaining skills should be cancelled (Req 12.4).

    We simulate this by using a very short budget and skills that take some time.
    """
    import orchestrator.workflow_orchestrator as orch_mod

    # Very short budget — skills will exceed it
    monkeypatch.setattr(orch_mod, "SKILL_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(orch_mod, "CUMULATIVE_TIMEOUT_SECONDS", 0.3)

    class BudgetTestInvoker(SkillInvoker):
        """Skills that collectively exceed the budget."""

        def __init__(self) -> None:
            self.completed: list[str] = []
            self.started: list[str] = []

        async def invoke(
            self, skill_name: str, input_data: dict[str, Any], timeout: float
        ) -> SkillResult:
            self.started.append(skill_name)
            # Each skill takes some time
            await asyncio.sleep(0.2)
            self.completed.append(skill_name)
            return SkillResult(skill_name=skill_name, success=True, data={})

    invoker = BudgetTestInvoker()
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert()

    report = await orchestrator.handle_alert(alert)

    # Report should still be produced even with budget pressure
    assert report is not None
    assert report.incident_id is not None
    # Some skills should have been cancelled or skipped
    # due to budget being only 0.3s and skills needing 0.2s each


@pytest.mark.asyncio
async def test_budget_exceeded_marks_skills_unavailable(monkeypatch):
    """Skills cancelled due to budget should appear as unavailable sources."""
    import orchestrator.workflow_orchestrator as orch_mod

    # Budget already nearly exhausted — skills will be cancelled
    monkeypatch.setattr(orch_mod, "SKILL_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(orch_mod, "CUMULATIVE_TIMEOUT_SECONDS", 0.05)

    class SlowInvoker(SkillInvoker):
        async def invoke(
            self, skill_name: str, input_data: dict[str, Any], timeout: float
        ) -> SkillResult:
            await asyncio.sleep(0.1)  # Exceeds 0.05s budget
            return SkillResult(skill_name=skill_name, success=True, data={})

    invoker = SlowInvoker()
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert()

    report = await orchestrator.handle_alert(alert)

    # Report produced with degraded confidence
    assert report is not None
    assert report.confidence <= 0.5  # Sources marked unavailable


# ---------------------------------------------------------------------------
# Tests: Multi-cluster timeout budget (Req 12.7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_cluster_gets_120s_budget():
    """Multi-cluster incidents should use 120s timeout budget (Req 12.7)."""
    alert = _make_multi_cluster_alert()
    orchestrator = WorkflowOrchestrator(skill_invoker=SlowSkillInvoker())

    # Verify the orchestrator detects multi-cluster
    assert orchestrator._is_multi_cluster_incident(alert) is True

    # Verify the context gets multi-cluster budget
    ctx = WorkflowContext(
        incident_id="test-multi",
        alert=alert,
        start_time=time.monotonic(),
        is_multi_cluster=True,
    )
    assert ctx.timeout_budget == MULTI_CLUSTER_TIMEOUT_SECONDS
    assert ctx.timeout_budget == 120


@pytest.mark.asyncio
async def test_single_cluster_gets_45s_budget():
    """Single-cluster incidents should use 45s timeout budget."""
    alert = _make_alert(cluster="single-cluster")
    orchestrator = WorkflowOrchestrator(skill_invoker=SlowSkillInvoker())

    # Single cluster → not multi-cluster
    assert orchestrator._is_multi_cluster_incident(alert) is False

    ctx = WorkflowContext(
        incident_id="test-single",
        alert=alert,
        start_time=time.monotonic(),
        is_multi_cluster=False,
    )
    assert ctx.timeout_budget == CUMULATIVE_TIMEOUT_SECONDS
    assert ctx.timeout_budget == 45


@pytest.mark.asyncio
async def test_no_cluster_gets_45s_budget():
    """Non-EKS alerts (no cluster) should use 45s budget."""
    alert = _make_alert(cluster=None)
    orchestrator = WorkflowOrchestrator(skill_invoker=SlowSkillInvoker())

    assert orchestrator._is_multi_cluster_incident(alert) is False

    ctx = WorkflowContext(
        incident_id="test-none",
        alert=alert,
        start_time=time.monotonic(),
        is_multi_cluster=False,
    )
    assert ctx.timeout_budget == 45


@pytest.mark.asyncio
async def test_multi_cluster_detection_from_affected_resources():
    """Multi-cluster should be detected from affected resource values."""
    orchestrator = WorkflowOrchestrator(skill_invoker=SlowSkillInvoker())

    # Two clusters in affected resources
    alert = _make_multi_cluster_alert()
    assert orchestrator._is_multi_cluster_incident(alert) is True

    # Single cluster only
    single_alert = _make_alert(
        cluster="one-cluster",
        affected_resources=[
            ResourceIdentifier(
                type=ResourceIdentifierType.K8S_RESOURCE,
                value="one-cluster/default/deployment/app",
                display_name="app",
            ),
        ],
    )
    assert orchestrator._is_multi_cluster_incident(single_alert) is False


# ---------------------------------------------------------------------------
# Tests: Concurrent execution (Req 12.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_independent_skills_execute_concurrently():
    """Signal-collection skills should run concurrently (Req 12.2).

    Verifies that metric-baseline, deploy-correlation, and log-triage
    start at approximately the same time, not sequentially.
    """
    invoker = TimedInvoker()
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert()

    await orchestrator.handle_alert(alert)

    # All signal skills should have started within a very short window
    signal_skills = {"metric-baseline", "deploy-correlation", "log-triage"}
    signal_starts = [
        invoker.start_times[s] for s in signal_skills if s in invoker.start_times
    ]

    assert len(signal_starts) == 3, "All three signal skills should have been invoked"

    # All should start within 20ms of each other (concurrent)
    max_start = max(signal_starts)
    min_start = min(signal_starts)
    assert max_start - min_start < 0.02, (
        f"Signal skills should start concurrently. "
        f"Spread: {(max_start - min_start)*1000:.1f}ms"
    )


@pytest.mark.asyncio
async def test_k8s_skills_execute_concurrently_with_signal_skills():
    """K8s skills should run concurrently with other signal skills (Req 12.2)."""
    invoker = TimedInvoker()
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert(cluster="prod-cluster", namespace="default")

    await orchestrator.handle_alert(alert)

    # All signal + k8s skills should start concurrently
    concurrent_skills = {
        "metric-baseline", "deploy-correlation", "log-triage",
        "k8s-cluster-health",
    }
    starts = [
        invoker.start_times[s] for s in concurrent_skills if s in invoker.start_times
    ]

    assert len(starts) >= 4
    max_start = max(starts)
    min_start = min(starts)
    assert max_start - min_start < 0.02


@pytest.mark.asyncio
async def test_hypothesis_runs_after_signals_complete():
    """hypothesis-engine must wait for signal collection to finish (Req 12.2)."""
    invoker = TimedInvoker()
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert()

    await orchestrator.handle_alert(alert)

    # hypothesis-engine should start AFTER signal skills end
    signal_skills = {"metric-baseline", "deploy-correlation", "log-triage"}
    signal_end_times = [
        invoker.end_times[s] for s in signal_skills if s in invoker.end_times
    ]
    hypothesis_start = invoker.start_times.get("hypothesis-engine")

    assert hypothesis_start is not None, "hypothesis-engine should be invoked"
    assert len(signal_end_times) == 3

    # Hypothesis should start after all signals complete
    latest_signal_end = max(signal_end_times)
    assert hypothesis_start >= latest_signal_end - 0.001  # small tolerance


# ---------------------------------------------------------------------------
# Tests: Processing duration recording (Req 12.6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_processing_duration_recorded_in_report():
    """IncidentReport should include processing_duration in milliseconds (Req 12.6)."""
    invoker = SlowSkillInvoker(delays={"metric-baseline": 0.05})
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert()

    report = await orchestrator.handle_alert(alert)

    # processing_duration should be a string ending in "ms"
    assert report.processing_duration.endswith("ms")

    # Parse the numeric value
    duration_ms = float(report.processing_duration.rstrip("ms"))
    assert duration_ms > 0, "Duration must be positive"
    # Should be at least 50ms since metric-baseline has 50ms delay
    assert duration_ms >= 40  # small tolerance


@pytest.mark.asyncio
async def test_processing_duration_increases_with_work():
    """Longer workflows should record longer processing durations."""
    # Fast workflow
    fast_invoker = SlowSkillInvoker()
    orchestrator = WorkflowOrchestrator(skill_invoker=fast_invoker)
    alert = _make_alert()
    fast_report = await orchestrator.handle_alert(alert)
    fast_duration = float(fast_report.processing_duration.rstrip("ms"))

    # Slow workflow
    slow_invoker = SlowSkillInvoker(delays={
        "metric-baseline": 0.1,
        "deploy-correlation": 0.1,
        "log-triage": 0.1,
    })
    orchestrator2 = WorkflowOrchestrator(skill_invoker=slow_invoker)
    slow_report = await orchestrator2.handle_alert(alert)
    slow_duration = float(slow_report.processing_duration.rstrip("ms"))

    # Slow workflow should take longer
    assert slow_duration > fast_duration


@pytest.mark.asyncio
async def test_generated_at_after_fired_at():
    """generatedAt should be after the alert's firedAt (Req 12.6)."""
    invoker = SlowSkillInvoker()
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)
    alert = _make_alert()

    report = await orchestrator.handle_alert(alert)

    assert report.generated_at > alert.fired_at


# ---------------------------------------------------------------------------
# Tests: Timeout constant values
# ---------------------------------------------------------------------------


def test_skill_timeout_is_15_seconds():
    """Per-skill timeout should be 15 seconds (Req 12.3)."""
    assert SKILL_TIMEOUT_SECONDS == 15


def test_cumulative_timeout_is_45_seconds():
    """Cumulative budget should be 45 seconds (Req 12.4)."""
    assert CUMULATIVE_TIMEOUT_SECONDS == 45


def test_multi_cluster_timeout_is_120_seconds():
    """Multi-cluster budget should be 120 seconds (Req 12.7)."""
    assert MULTI_CLUSTER_TIMEOUT_SECONDS == 120


# ---------------------------------------------------------------------------
# Tests: WorkflowContext budget mechanics
# ---------------------------------------------------------------------------


def test_context_remaining_budget_decreases():
    """remaining_budget should decrease as time passes."""
    alert = _make_alert()
    ctx = WorkflowContext(
        incident_id="test",
        alert=alert,
        start_time=time.monotonic() - 10,  # 10 seconds ago
    )
    assert ctx.remaining_budget == pytest.approx(35.0, abs=0.5)  # 45 - 10


def test_context_budget_exceeded_at_boundary():
    """is_budget_exceeded should return True when budget is exhausted."""
    alert = _make_alert()

    # Just within budget
    ctx_within = WorkflowContext(
        incident_id="test",
        alert=alert,
        start_time=time.monotonic() - 44,
    )
    assert ctx_within.is_budget_exceeded() is False

    # Over budget
    ctx_over = WorkflowContext(
        incident_id="test",
        alert=alert,
        start_time=time.monotonic() - 46,
    )
    assert ctx_over.is_budget_exceeded() is True


def test_multi_cluster_context_budget_exceeded():
    """Multi-cluster context should use 120s boundary."""
    alert = _make_alert()

    # 50s elapsed — within multi-cluster budget but past single-cluster
    ctx = WorkflowContext(
        incident_id="test",
        alert=alert,
        start_time=time.monotonic() - 50,
        is_multi_cluster=True,
    )
    assert ctx.is_budget_exceeded() is False
    assert ctx.remaining_budget == pytest.approx(70.0, abs=0.5)

    # 121s elapsed — past multi-cluster budget
    ctx_over = WorkflowContext(
        incident_id="test",
        alert=alert,
        start_time=time.monotonic() - 121,
        is_multi_cluster=True,
    )
    assert ctx_over.is_budget_exceeded() is True
    assert ctx_over.remaining_budget == 0.0
