"""Tests for the end-to-end pipeline wiring (Task 18.1).

Verifies:
- All components are importable and connected
- Pipeline function signatures are correct
- Skill invocation order matches design workflow sequence
- NormalizedAlert flows through entire pipeline to Slack message
- Interactive triage mode can invoke full orchestrator workflow
- SubprocessSkillInvoker knows all skills and resolves paths

These tests use mocks — no external processes or MCPs are invoked.

Requirements: 10.5, 12.1, 12.2
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.pipeline import (
    PipelineResult,
    format_incident_report,
    ingest_raw_alarm,
    run_pipeline,
    triage_with_full_workflow,
)
from orchestrator.skill_invoker_impl import (
    SKILL_SCRIPT_MAP,
    SubprocessSkillInvoker,
)
from orchestrator.slack_delivery import DeliveryResult, SlackDeliveryService
from orchestrator.workflow_orchestrator import (
    SkillInvoker,
    SkillResult,
    WorkflowOrchestrator,
)
from orchestrator.interactive_triage import start_investigation, clear_sessions
from skills.shared.models import (
    AlertSource,
    AlertState,
    NormalizedAlert,
    ResourceIdentifier,
    ResourceIdentifierType,
    Severity,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_triage_sessions():
    """Ensure triage session store is clean between tests."""
    clear_sessions()
    yield
    clear_sessions()


def _make_normalized_alert(**overrides: Any) -> NormalizedAlert:
    """Create a NormalizedAlert for testing."""
    defaults = {
        "id": "test-alert-001",
        "source": AlertSource.CLOUDWATCH,
        "source_alarm_id": "arn:aws:cloudwatch:us-east-1:123456789012:alarm:TestAlarm",
        "severity": Severity.HIGH,
        "title": "High CPU on prod-service",
        "description": "CPU utilization exceeded 90%",
        "state": AlertState.FIRING,
        "fired_at": datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc),
        "affected_resources": [
            ResourceIdentifier(
                type=ResourceIdentifierType.ARN,
                value="arn:aws:ec2:us-east-1:123456789012:instance/i-1234567890abcdef0",
                displayName="prod-service-instance",
            )
        ],
        "region": "us-east-1",
        "raw_payload": {"AlarmName": "TestAlarm", "NewStateValue": "ALARM"},
    }
    defaults.update(overrides)
    return NormalizedAlert(**defaults)


class MockSkillInvoker(SkillInvoker):
    """Mock SkillInvoker that records invocations and returns configurable results."""

    def __init__(self, results: dict[str, SkillResult] | None = None):
        self.invocations: list[tuple[str, dict[str, Any], float]] = []
        self._results = results or {}

    async def invoke(
        self, skill_name: str, input_data: dict[str, Any], timeout: float
    ) -> SkillResult:
        self.invocations.append((skill_name, input_data, timeout))
        if skill_name in self._results:
            return self._results[skill_name]
        # Default: return success with empty data
        return SkillResult(skill_name=skill_name, success=True, data={})


class MockSlackClient:
    """Mock Slack MCP client that records posted messages."""

    def __init__(self, should_fail: bool = False):
        self.messages_posted: list[tuple[str, dict[str, Any]]] = []
        self._should_fail = should_fail

    async def post_message(
        self, channel: str, message: dict[str, Any]
    ) -> dict[str, Any]:
        self.messages_posted.append((channel, message))
        if self._should_fail:
            raise Exception("Slack delivery failed")
        return {"ok": True, "ts": "1705312200.000100"}

    async def update_message(
        self, channel: str, ts: str, message: dict[str, Any]
    ) -> dict[str, Any]:
        return {"ok": True, "ts": ts}


# ---------------------------------------------------------------------------
# Test: Module imports and wiring
# ---------------------------------------------------------------------------


class TestModuleImports:
    """Verify all pipeline components are importable and connected."""

    def test_pipeline_module_imports(self):
        """All pipeline functions are importable from orchestrator package."""
        from orchestrator import (
            PipelineResult,
            SubprocessSkillInvoker,
            SKILL_SCRIPT_MAP,
            run_pipeline,
            triage_with_full_workflow,
            ingest_raw_alarm,
            format_incident_report,
        )
        assert PipelineResult is not None
        assert SubprocessSkillInvoker is not None
        assert callable(run_pipeline)
        assert callable(triage_with_full_workflow)
        assert callable(ingest_raw_alarm)
        assert callable(format_incident_report)

    def test_orchestrator_exports_all_core_modules(self):
        """Orchestrator __init__.py exports all core module symbols."""
        from orchestrator import (
            WorkflowOrchestrator,
            SlackDeliveryService,
            start_investigation,
            SubprocessSkillInvoker,
            run_pipeline,
        )
        # All exist and are the correct types
        assert WorkflowOrchestrator is not None
        assert SlackDeliveryService is not None
        assert callable(start_investigation)
        assert SubprocessSkillInvoker is not None
        assert callable(run_pipeline)

    def test_skill_invoker_impl_knows_all_skills(self):
        """SubprocessSkillInvoker has mappings for all design-specified skills."""
        expected_skills = {
            "alert-ingestion",
            "metric-baseline",
            "deploy-correlation",
            "log-triage",
            "hypothesis-engine",
            "escalation-decision",
            "incident-summary-format",
            "evidence-provenance",
            "k8s-cluster-health",
            "pod-failure-triage",
            "node-condition-check",
            "eks-addon-status",
        }
        assert set(SKILL_SCRIPT_MAP.keys()) == expected_skills

    def test_pipeline_result_dataclass(self):
        """PipelineResult has all expected fields."""
        result = PipelineResult(success=True)
        assert result.success is True
        assert result.incident_report is None
        assert result.slack_message is None
        assert result.delivery_result is None
        assert result.error is None
        assert result.processing_duration_ms == 0.0


# ---------------------------------------------------------------------------
# Test: SubprocessSkillInvoker
# ---------------------------------------------------------------------------


class TestSubprocessSkillInvoker:
    """Verify SubprocessSkillInvoker skill resolution and configuration."""

    def test_default_skills_directory(self):
        """Default skills dir is relative to project root."""
        invoker = SubprocessSkillInvoker()
        assert invoker.skills_dir.name == "skills"
        assert invoker.skills_dir.exists()

    def test_resolve_known_skill_paths(self):
        """All skill scripts can be resolved to existing paths."""
        invoker = SubprocessSkillInvoker()
        for skill_name in SKILL_SCRIPT_MAP:
            path = invoker._resolve_script_path(skill_name)
            assert path.exists(), f"Script for {skill_name} not found at {path}"
            assert path.suffix == ".py"

    def test_resolve_unknown_skill_raises(self):
        """Attempting to resolve an unknown skill raises FileNotFoundError."""
        invoker = SubprocessSkillInvoker()
        with pytest.raises(FileNotFoundError, match="Unknown skill"):
            invoker._resolve_script_path("nonexistent-skill")

    def test_custom_skills_dir(self, tmp_path: Path):
        """Custom skills directory can be configured."""
        invoker = SubprocessSkillInvoker(skills_dir=tmp_path)
        assert invoker.skills_dir == tmp_path


# ---------------------------------------------------------------------------
# Test: Pipeline function signatures and flow
# ---------------------------------------------------------------------------


class TestPipelineFunctionSignatures:
    """Verify pipeline functions have correct signatures and behavior."""

    @pytest.mark.asyncio
    async def test_ingest_raw_alarm_with_successful_skill(self):
        """ingest_raw_alarm returns NormalizedAlert on success."""
        alert_data = _make_normalized_alert().model_dump(by_alias=True, mode="json")
        mock_invoker = MockSkillInvoker(results={
            "alert-ingestion": SkillResult(
                skill_name="alert-ingestion",
                success=True,
                data={"result": alert_data},
            )
        })

        result = await ingest_raw_alarm(
            {"source": "cloudwatch", "payload": {"AlarmName": "Test"}},
            mock_invoker,
        )

        assert result is not None
        assert isinstance(result, NormalizedAlert)
        assert result.id == "test-alert-001"
        # Verify the skill was invoked
        assert len(mock_invoker.invocations) == 1
        assert mock_invoker.invocations[0][0] == "alert-ingestion"

    @pytest.mark.asyncio
    async def test_ingest_raw_alarm_failure_returns_none(self):
        """ingest_raw_alarm returns None when skill fails."""
        mock_invoker = MockSkillInvoker(results={
            "alert-ingestion": SkillResult(
                skill_name="alert-ingestion",
                success=False,
                error="Invalid payload",
            )
        })

        result = await ingest_raw_alarm(
            {"source": "cloudwatch", "payload": {}},
            mock_invoker,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_format_incident_report_with_successful_skill(self):
        """format_incident_report returns Slack message on success."""
        from skills.shared.models import BlastRadius

        alert = _make_normalized_alert()
        report = MagicMock()
        report.model_dump.return_value = {"incident_id": "test-123"}
        report.severity = Severity.HIGH
        report.alert = alert
        report.incident_id = "test-123"
        report.processing_duration = "500ms"

        expected_message = {"blocks": [{"type": "section"}], "text": "Test"}
        mock_invoker = MockSkillInvoker(results={
            "incident-summary-format": SkillResult(
                skill_name="incident-summary-format",
                success=True,
                data={"result": expected_message},
            )
        })

        result = await format_incident_report(report, mock_invoker)

        assert result == expected_message
        assert len(mock_invoker.invocations) == 1
        assert mock_invoker.invocations[0][0] == "incident-summary-format"

    @pytest.mark.asyncio
    async def test_format_incident_report_fallback_on_failure(self):
        """format_incident_report produces fallback message when skill fails."""
        alert = _make_normalized_alert()
        report = MagicMock()
        report.model_dump.return_value = {"incident_id": "test-123"}
        report.severity = Severity.HIGH
        report.alert = alert
        report.incident_id = "test-123"
        report.processing_duration = "500ms"

        mock_invoker = MockSkillInvoker(results={
            "incident-summary-format": SkillResult(
                skill_name="incident-summary-format",
                success=False,
                error="Formatting error",
            )
        })

        result = await format_incident_report(report, mock_invoker)

        assert result is not None
        assert "text" in result
        assert "blocks" in result
        # Fallback message should mention the alert title
        assert "High CPU on prod-service" in result["text"]


# ---------------------------------------------------------------------------
# Test: Full pipeline flow
# ---------------------------------------------------------------------------


class TestFullPipeline:
    """Verify the full pipeline connects all components correctly."""

    @pytest.mark.asyncio
    async def test_run_pipeline_end_to_end_with_mock(self):
        """Full pipeline executes: ingestion → orchestrator → format → delivery."""
        alert = _make_normalized_alert()
        alert_data = alert.model_dump(by_alias=True, mode="json")

        mock_invoker = MockSkillInvoker(results={
            "alert-ingestion": SkillResult(
                skill_name="alert-ingestion",
                success=True,
                data={"result": alert_data},
            ),
            "incident-summary-format": SkillResult(
                skill_name="incident-summary-format",
                success=True,
                data={"result": {"blocks": [], "text": "Incident report"}},
            ),
        })

        mock_slack = MockSlackClient()

        result = await run_pipeline(
            raw_payload={"source": "cloudwatch", "payload": {"AlarmName": "Test"}},
            skill_invoker=mock_invoker,
            slack_client=mock_slack,
            slack_channel="#incidents",
        )

        assert result.success is True
        assert result.incident_report is not None
        assert result.slack_message is not None
        assert result.delivery_result is not None
        assert result.delivery_result.success is True
        assert result.processing_duration_ms > 0

        # Verify Slack was called with the formatted message
        assert len(mock_slack.messages_posted) == 1
        channel, message = mock_slack.messages_posted[0]
        assert channel == "#incidents"

    @pytest.mark.asyncio
    async def test_run_pipeline_skill_invocation_order(self):
        """Pipeline invokes skills in the correct workflow sequence."""
        alert = _make_normalized_alert()
        alert_data = alert.model_dump(by_alias=True, mode="json")

        mock_invoker = MockSkillInvoker(results={
            "alert-ingestion": SkillResult(
                skill_name="alert-ingestion",
                success=True,
                data={"result": alert_data},
            ),
            "incident-summary-format": SkillResult(
                skill_name="incident-summary-format",
                success=True,
                data={"result": {"blocks": [], "text": "report"}},
            ),
        })

        await run_pipeline(
            raw_payload={"source": "cloudwatch", "payload": {}},
            skill_invoker=mock_invoker,
        )

        # Extract the ordered list of skills invoked
        invoked_skills = [name for name, _, _ in mock_invoker.invocations]

        # First invocation should be alert-ingestion
        assert invoked_skills[0] == "alert-ingestion"

        # Signal collection skills should come next (concurrent, so order
        # among them may vary, but they all come before hypothesis-engine)
        signal_skills = {"metric-baseline", "deploy-correlation", "log-triage"}
        # Find where hypothesis-engine appears
        if "hypothesis-engine" in invoked_skills:
            hyp_idx = invoked_skills.index("hypothesis-engine")
            # All signal skills should appear before hypothesis-engine
            for skill in signal_skills:
                if skill in invoked_skills:
                    assert invoked_skills.index(skill) < hyp_idx, (
                        f"{skill} should come before hypothesis-engine"
                    )

        # incident-summary-format should be last (formatting step)
        assert invoked_skills[-1] == "incident-summary-format"

    @pytest.mark.asyncio
    async def test_run_pipeline_without_slack_client(self):
        """Pipeline succeeds without Slack delivery when no client provided."""
        alert = _make_normalized_alert()
        alert_data = alert.model_dump(by_alias=True, mode="json")

        mock_invoker = MockSkillInvoker(results={
            "alert-ingestion": SkillResult(
                skill_name="alert-ingestion",
                success=True,
                data={"result": alert_data},
            ),
            "incident-summary-format": SkillResult(
                skill_name="incident-summary-format",
                success=True,
                data={"result": {"blocks": [], "text": "report"}},
            ),
        })

        result = await run_pipeline(
            raw_payload={"source": "cloudwatch", "payload": {}},
            skill_invoker=mock_invoker,
            # No slack_client — delivery should be skipped
        )

        assert result.success is True
        assert result.incident_report is not None
        assert result.delivery_result is None

    @pytest.mark.asyncio
    async def test_run_pipeline_ingestion_failure(self):
        """Pipeline returns failure when alert ingestion fails."""
        mock_invoker = MockSkillInvoker(results={
            "alert-ingestion": SkillResult(
                skill_name="alert-ingestion",
                success=False,
                error="Missing required fields",
            )
        })

        result = await run_pipeline(
            raw_payload={"source": "unknown", "payload": {}},
            skill_invoker=mock_invoker,
        )

        assert result.success is False
        assert "ingestion failed" in result.error.lower()
        assert result.incident_report is None


# ---------------------------------------------------------------------------
# Test: Interactive triage → full orchestrator workflow
# ---------------------------------------------------------------------------


class TestTriageWithFullWorkflow:
    """Verify interactive triage can trigger the full orchestrator workflow."""

    @pytest.mark.asyncio
    async def test_triage_with_full_workflow_end_to_end(self):
        """triage_with_full_workflow synthesizes alert and runs full pipeline."""
        mock_invoker = MockSkillInvoker()
        mock_slack = MockSlackClient()

        session, pipeline_result = await triage_with_full_workflow(
            prompt="our prod-cluster is throwing 503 errors in us-east-1",
            skill_invoker=mock_invoker,
            slack_client=mock_slack,
            slack_channel="#incidents",
        )

        # Session should be created with synthesized alert
        assert session is not None
        assert session.session_id is not None
        assert session.normalized_alert is not None
        assert session.normalized_alert.region == "us-east-1"

        # Pipeline should complete
        assert pipeline_result.success is True
        assert pipeline_result.incident_report is not None

        # Skills should have been invoked (at minimum signal collection)
        invoked_skills = {name for name, _, _ in mock_invoker.invocations}
        assert "metric-baseline" in invoked_skills
        assert "log-triage" in invoked_skills

    @pytest.mark.asyncio
    async def test_triage_preserves_session_state(self):
        """Triage session is preserved for follow-up queries."""
        mock_invoker = MockSkillInvoker()

        session, _ = await triage_with_full_workflow(
            prompt="high latency in payment-service",
            skill_invoker=mock_invoker,
        )

        # Session should exist in the store for follow-ups
        from orchestrator.interactive_triage import _get_session
        stored_session = _get_session(session.session_id)
        assert stored_session is session

    @pytest.mark.asyncio
    async def test_triage_with_k8s_context_invokes_k8s_skills(self):
        """When triage mentions a cluster, K8s skills are invoked."""
        mock_invoker = MockSkillInvoker()

        session, _ = await triage_with_full_workflow(
            prompt="pods crashing in prod-eks-cluster namespace payments",
            skill_invoker=mock_invoker,
        )

        # The synthesized alert should have cluster context
        assert session.normalized_alert.cluster is not None

        # K8s-related skills should be invoked
        invoked_skills = {name for name, _, _ in mock_invoker.invocations}
        assert "k8s-cluster-health" in invoked_skills


# ---------------------------------------------------------------------------
# Test: Workflow sequence verification
# ---------------------------------------------------------------------------


class TestWorkflowSequence:
    """Verify skill invocation order matches the design workflow sequence."""

    @pytest.mark.asyncio
    async def test_orchestrator_workflow_phases(self):
        """Orchestrator executes skills in the correct dependency order."""
        # Track invocation order
        invocation_order: list[str] = []

        class OrderTrackingInvoker(SkillInvoker):
            async def invoke(
                self, skill_name: str, input_data: dict[str, Any], timeout: float
            ) -> SkillResult:
                invocation_order.append(skill_name)
                return SkillResult(skill_name=skill_name, success=True, data={})

        invoker = OrderTrackingInvoker()
        orchestrator = WorkflowOrchestrator(invoker)
        alert = _make_normalized_alert()

        await orchestrator.handle_alert(alert)

        # Phase 1: Signal collection (concurrent — order among them varies)
        signal_skills = {"metric-baseline", "deploy-correlation", "log-triage"}
        signal_indices = [
            i for i, name in enumerate(invocation_order)
            if name in signal_skills
        ]

        # Phase 2: Hypothesis engine comes after signal collection
        if "hypothesis-engine" in invocation_order:
            hyp_idx = invocation_order.index("hypothesis-engine")
            for idx in signal_indices:
                assert idx < hyp_idx, (
                    "Signal collection skills must precede hypothesis-engine"
                )

        # Phase 3: Escalation + evidence after hypothesis
        post_hypothesis = {"escalation-decision", "evidence-provenance"}
        if "hypothesis-engine" in invocation_order:
            hyp_idx = invocation_order.index("hypothesis-engine")
            for skill in post_hypothesis:
                if skill in invocation_order:
                    assert invocation_order.index(skill) > hyp_idx, (
                        f"{skill} should come after hypothesis-engine"
                    )

    @pytest.mark.asyncio
    async def test_k8s_skills_skipped_for_non_eks_alerts(self):
        """Orchestrator skips K8s skills when alert has no cluster field."""
        mock_invoker = MockSkillInvoker()
        orchestrator = WorkflowOrchestrator(mock_invoker)

        # Alert without cluster field
        alert = _make_normalized_alert(cluster=None, namespace=None)
        await orchestrator.handle_alert(alert)

        invoked_skills = {name for name, _, _ in mock_invoker.invocations}
        k8s_skills = {
            "k8s-cluster-health",
            "pod-failure-triage",
            "node-condition-check",
            "eks-addon-status",
        }
        assert invoked_skills.isdisjoint(k8s_skills), (
            "K8s skills should not be invoked for non-EKS alerts"
        )

    @pytest.mark.asyncio
    async def test_k8s_skills_invoked_for_eks_alerts(self):
        """Orchestrator invokes K8s skills when alert has cluster field."""
        mock_invoker = MockSkillInvoker()
        orchestrator = WorkflowOrchestrator(mock_invoker)

        # Alert with cluster field
        alert = _make_normalized_alert(cluster="prod-cluster")
        await orchestrator.handle_alert(alert)

        invoked_skills = {name for name, _, _ in mock_invoker.invocations}
        assert "k8s-cluster-health" in invoked_skills
