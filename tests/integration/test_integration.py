"""Integration tests for CodeBlue AI end-to-end pipeline (Task 18.2).

Tests complete pipeline flows with mocked MCP responses:
1. Alarm → normalized alert → signals → hypothesis → Slack message
2. Graceful degradation when individual MCPs are unavailable
3. Timeout enforcement (15s per skill, 45s total budget)
4. Slack retry logic with simulated failures
5. Interactive triage session across multiple follow-ups

Requirements: 11.1, 11.2, 12.1, 12.4, 12.5, 7.5
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from orchestrator.interactive_triage import (
    TriageSession,
    clear_sessions,
    follow_up,
    invoke_skill,
    register_skill,
    start_investigation,
)
from orchestrator.pipeline import (
    PipelineResult,
    run_pipeline,
    triage_with_full_workflow,
)
from orchestrator.slack_delivery import (
    DeliveryResult,
    SlackDeliveryService,
    SlackDeliveryError,
)
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
# Fixtures and Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_triage():
    """Clean triage sessions between tests."""
    clear_sessions()
    yield
    clear_sessions()


def _make_alert(**overrides: Any) -> NormalizedAlert:
    """Create a test NormalizedAlert with sensible defaults."""
    defaults: dict[str, Any] = {
        "id": "integ-alert-001",
        "source": AlertSource.CLOUDWATCH,
        "source_alarm_id": "arn:aws:cloudwatch:us-east-1:111:alarm:HighCPU",
        "severity": Severity.HIGH,
        "title": "High CPU on prod-api",
        "description": "CPUUtilization > 90% for 5 minutes",
        "state": AlertState.FIRING,
        "fired_at": datetime(2024, 3, 1, 12, 0, 0, tzinfo=timezone.utc),
        "affected_resources": [
            ResourceIdentifier(
                type=ResourceIdentifierType.ARN,
                value="arn:aws:ec2:us-east-1:111:instance/i-abc123",
                displayName="prod-api-instance",
            )
        ],
        "region": "us-east-1",
        "metric_name": "CPUUtilization",
        "metric_namespace": "AWS/EC2",
        "dimensions": {"InstanceId": "i-abc123"},
        "threshold": 90.0,
        "current_value": 95.2,
        "raw_payload": {"AlarmName": "HighCPU", "NewStateValue": "ALARM"},
    }
    defaults.update(overrides)
    return NormalizedAlert(**defaults)


# ---------------------------------------------------------------------------
# Mock Skill Invoker — returns realistic data per skill
# ---------------------------------------------------------------------------


def _realistic_metric_baseline() -> dict[str, Any]:
    """Realistic metric-baseline skill output."""
    return {
        "metricName": "CPUUtilization",
        "namespace": "AWS/EC2",
        "dimensions": {"InstanceId": "i-abc123"},
        "timeRange": {
            "start": "2024-03-01T11:00:00Z",
            "end": "2024-03-01T12:00:00Z",
        },
        "currentMean": 95.2,
        "currentP95": 98.1,
        "currentMax": 99.8,
        "baselineMean": 42.0,
        "baselineP95": 65.0,
        "baselineP99": 72.0,
        "baselineStdDev": 8.5,
        "deviationFactor": 6.26,
        "classification": "critical",
        "confidence": 0.92,
        "anomalyStartTime": "2024-03-01T11:45:00Z",
        "dataPoints": [],
        "baselineDataPoints": [],
    }


def _realistic_deploy_correlation() -> dict[str, Any]:
    """Realistic deploy-correlation skill output."""
    return {
        "changes": [
            {
                "id": "change-001",
                "type": "deployment",
                "source": "kubernetes",
                "timestamp": "2024-03-01T11:42:00Z",
                "actor": "deploy-bot",
                "description": "Rolled out prod-api v2.3.1",
                "affectedResource": "deployment/prod-api",
                "details": {"image": "prod-api:v2.3.1"},
                "temporalProximity": 180,
                "resourceOverlap": True,
                "correlationScore": 0.87,
            }
        ],
        "lookbackWindow": "24h",
        "affectedResources": [
            {"type": "arn", "value": "arn:aws:ec2:us-east-1:111:instance/i-abc123", "displayName": "prod-api-instance"}
        ],
    }


def _realistic_log_triage() -> dict[str, Any]:
    """Realistic log-triage skill output."""
    return {
        "findings": [
            {
                "pattern": "java.lang.OutOfMemoryError",
                "count": 47,
                "firstSeen": "2024-03-01T11:46:00Z",
                "lastSeen": "2024-03-01T11:59:00Z",
                "isNew": True,
                "severity": "fatal",
                "sampleLogLines": [
                    "2024-03-01T11:46:12Z ERROR java.lang.OutOfMemoryError: Java heap space",
                    "2024-03-01T11:47:03Z ERROR java.lang.OutOfMemoryError: GC overhead limit exceeded",
                ],
                "logGroup": "/aws/containerinsights/prod-cluster/application",
                "logStream": "prod-api-pod-xyz",
            }
        ],
        "queriedLogGroups": ["/aws/containerinsights/prod-cluster/application"],
        "timeRange": {
            "start": "2024-03-01T11:00:00Z",
            "end": "2024-03-01T12:00:00Z",
        },
        "totalErrorCount": 47,
        "baselineErrorCount": 2,
    }


def _realistic_hypothesis() -> dict[str, Any]:
    """Realistic hypothesis-engine skill output."""
    return {
        "hypotheses": [
            {
                "rank": 1,
                "title": "Memory leak in prod-api v2.3.1 causing OOMKilled",
                "description": "Deployment of v2.3.1 at 11:42 preceded OOM errors by 4 min",
                "confidence": 0.85,
                "supportingEvidence": [
                    {"claim": "OOM errors started after deploy", "source": "log-triage"}
                ],
                "contradictingEvidence": [],
                "suggestedVerification": ["Check heap dump", "Rollback to v2.3.0"],
            },
            {
                "rank": 2,
                "title": "Traffic spike exceeding resource limits",
                "description": "CPU at 95% may indicate unexpected load increase",
                "confidence": 0.45,
                "supportingEvidence": [
                    {"claim": "CPU critical deviation", "source": "metric-baseline"}
                ],
                "contradictingEvidence": [],
                "suggestedVerification": ["Check request rate metrics"],
            },
        ]
    }


def _realistic_escalation() -> dict[str, Any]:
    """Realistic escalation-decision skill output."""
    return {
        "escalate": True,
        "reason": "Critical severity with active OOM errors; blast radius includes prod-api",
        "urgency": "page_now",
        "suggestedResponders": ["platform-oncall", "prod-api-team"],
    }


def _realistic_summary() -> dict[str, Any]:
    """Realistic incident-summary-format skill output."""
    return {
        "result": {
            "text": "🔴 [CRITICAL] Memory leak in prod-api v2.3.1",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "🔴 Incident: High CPU on prod-api"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": "*Severity:* Critical\n*Blast Radius:* 1 service in us-east-1"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": "*Hypothesis:* Memory leak in prod-api v2.3.1 (85% confidence)"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": "*Next Steps:*\n• Check heap dump\n• Rollback to v2.3.0"}},
                {"type": "context", "elements": [{"type": "mrkdwn", "text": "_CodeBlue AI | incident-id | 1200ms_"}]},
            ],
        }
    }


class RealisticSkillInvoker(SkillInvoker):
    """Skill invoker that returns realistic data for all skills.

    Tracks invocation order and supports configurable failures.
    """

    def __init__(
        self,
        *,
        failing_skills: set[str] | None = None,
        slow_skills: dict[str, float] | None = None,
    ):
        self.invocations: list[tuple[str, float]] = []
        self._failing_skills = failing_skills or set()
        self._slow_skills = slow_skills or {}

    async def invoke(
        self, skill_name: str, input_data: dict[str, Any], timeout: float
    ) -> SkillResult:
        self.invocations.append((skill_name, timeout))

        # Simulate slow skills
        if skill_name in self._slow_skills:
            await asyncio.sleep(self._slow_skills[skill_name])

        # Simulate failing skills
        if skill_name in self._failing_skills:
            return SkillResult(
                skill_name=skill_name,
                success=False,
                error=f"MCP connection failed for {skill_name}",
            )

        # Return realistic data based on skill name
        data = self._get_skill_data(skill_name)
        return SkillResult(skill_name=skill_name, success=True, data=data)

    def _get_skill_data(self, skill_name: str) -> dict[str, Any]:
        skill_data: dict[str, Any] = {
            "alert-ingestion": {"result": _make_alert().model_dump(by_alias=True, mode="json")},
            "metric-baseline": _realistic_metric_baseline(),
            "deploy-correlation": _realistic_deploy_correlation(),
            "log-triage": _realistic_log_triage(),
            "hypothesis-engine": _realistic_hypothesis(),
            "escalation-decision": _realistic_escalation(),
            "evidence-provenance": {"findings": []},
            "incident-summary-format": _realistic_summary(),
            "k8s-cluster-health": {},
            "pod-failure-triage": {},
            "node-condition-check": {},
            "eks-addon-status": {},
        }
        return skill_data.get(skill_name, {})


class MockSlackClient:
    """Mock Slack MCP client with configurable failure behavior."""

    def __init__(self, *, fail_count: int = 0):
        """Init mock Slack client.

        Args:
            fail_count: Number of initial failures before succeeding.
        """
        self.attempts: list[tuple[str, dict[str, Any]]] = []
        self._fail_count = fail_count
        self._call_count = 0

    async def post_message(
        self, channel: str, message: dict[str, Any]
    ) -> dict[str, Any]:
        self._call_count += 1
        self.attempts.append((channel, message))
        if self._call_count <= self._fail_count:
            raise SlackDeliveryError(
                f"Slack API error (attempt {self._call_count})", retryable=True
            )
        return {"ok": True, "ts": "1709294400.000100"}

    async def update_message(
        self, channel: str, ts: str, message: dict[str, Any]
    ) -> dict[str, Any]:
        return {"ok": True, "ts": ts}


# ---------------------------------------------------------------------------
# Test 1: Complete Pipeline — alarm → signals → hypothesis → Slack
# ---------------------------------------------------------------------------


class TestCompletePipeline:
    """Test the full end-to-end pipeline flow.

    Validates: Requirements 11.1, 12.1
    """

    @pytest.mark.asyncio
    async def test_full_pipeline_alarm_to_slack(self):
        """Complete pipeline: alarm → normalized alert → signals → hypothesis → Slack."""
        invoker = RealisticSkillInvoker()
        slack = MockSlackClient()

        result = await run_pipeline(
            raw_payload={"source": "cloudwatch", "payload": {"AlarmName": "HighCPU"}},
            skill_invoker=invoker,
            slack_client=slack,
            slack_channel="#incidents",
        )

        # Pipeline completes successfully
        assert result.success is True
        assert result.incident_report is not None
        assert result.slack_message is not None
        assert result.delivery_result is not None
        assert result.delivery_result.success is True
        assert result.processing_duration_ms > 0

        # Verify skill invocation order: ingestion → signals → hypothesis → report
        invoked = [name for name, _ in invoker.invocations]
        assert invoked[0] == "alert-ingestion"

        # Signal collection skills must precede hypothesis
        signal_skills = {"metric-baseline", "deploy-correlation", "log-triage"}
        if "hypothesis-engine" in invoked:
            hyp_idx = invoked.index("hypothesis-engine")
            for skill in signal_skills:
                if skill in invoked:
                    assert invoked.index(skill) < hyp_idx

        # Formatting is last skill
        assert invoked[-1] == "incident-summary-format"

        # Slack received the message
        assert len(slack.attempts) == 1
        channel, msg = slack.attempts[0]
        assert channel == "#incidents"
        assert "blocks" in msg or "text" in msg


    @pytest.mark.asyncio
    async def test_pipeline_report_contains_expected_fields(self):
        """IncidentReport has severity, blast radius, hypotheses, and confidence."""
        invoker = RealisticSkillInvoker()

        result = await run_pipeline(
            raw_payload={"source": "cloudwatch", "payload": {}},
            skill_invoker=invoker,
        )

        report = result.incident_report
        assert report is not None
        assert report.severity == Severity.HIGH
        assert report.blast_radius is not None
        assert report.blast_radius.region == "us-east-1"
        assert report.confidence > 0
        assert report.processing_duration is not None


# ---------------------------------------------------------------------------
# Test 2: Graceful Degradation — skills fail, report still produced
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    """Test that the pipeline produces reports even when skills fail.

    Validates: Requirements 11.1, 11.2
    """

    @pytest.mark.asyncio
    async def test_report_produced_with_single_skill_failure(self):
        """Report is produced when one signal skill fails."""
        invoker = RealisticSkillInvoker(failing_skills={"metric-baseline"})

        alert = _make_alert()
        orchestrator = WorkflowOrchestrator(invoker)
        report = await orchestrator.handle_alert(alert)

        # Report is still produced
        assert report is not None
        assert report.incident_id is not None

        # Confidence is reduced (0.2 per unavailable source)
        assert report.confidence < 0.8

        # Unavailable sources are recorded
        assert "metric-baseline" in report.unavailable_sources

    @pytest.mark.asyncio
    async def test_report_produced_with_multiple_skill_failures(self):
        """Report is produced when multiple skills fail."""
        invoker = RealisticSkillInvoker(
            failing_skills={"metric-baseline", "deploy-correlation"}
        )

        alert = _make_alert()
        orchestrator = WorkflowOrchestrator(invoker)
        report = await orchestrator.handle_alert(alert)

        assert report is not None
        # Each unavailable source reduces confidence by 0.2
        assert report.confidence <= 0.6
        assert "metric-baseline" in report.unavailable_sources
        assert "deploy-correlation" in report.unavailable_sources

    @pytest.mark.asyncio
    async def test_report_at_minimum_confidence_when_all_fail(self):
        """When all core signal sources fail, confidence is 0.1."""
        invoker = RealisticSkillInvoker(
            failing_skills={"metric-baseline", "deploy-correlation", "log-triage"}
        )

        alert = _make_alert()
        orchestrator = WorkflowOrchestrator(invoker)
        report = await orchestrator.handle_alert(alert)

        assert report is not None
        assert report.confidence == 0.1
        assert len(report.unavailable_sources) >= 3

    @pytest.mark.asyncio
    async def test_data_gaps_annotated_in_report(self):
        """Failed skills produce data gap annotations."""
        invoker = RealisticSkillInvoker(failing_skills={"log-triage"})

        alert = _make_alert()
        orchestrator = WorkflowOrchestrator(invoker)
        report = await orchestrator.handle_alert(alert)

        # Data gaps should be annotated
        assert report.data_gaps is not None
        gap_sources = [g["source_name"] for g in report.data_gaps]
        assert "log-triage" in gap_sources


# ---------------------------------------------------------------------------
# Test 3: Timeout Enforcement — per-skill and total budget
# ---------------------------------------------------------------------------


class TestTimeoutEnforcement:
    """Test timeout budget enforcement at skill and workflow level.

    Validates: Requirements 12.1, 12.4, 12.5
    """

    @pytest.mark.asyncio
    async def test_per_skill_timeout_enforced(self):
        """Skills exceeding 15s timeout are terminated and marked unavailable."""
        # Create an invoker where metric-baseline takes too long
        invoker = RealisticSkillInvoker(
            slow_skills={"metric-baseline": 20.0}  # 20s > 15s limit
        )

        alert = _make_alert()
        orchestrator = WorkflowOrchestrator(invoker)

        # Monkeypatch the timeout to a short value for test speed
        with patch(
            "orchestrator.workflow_orchestrator.SKILL_TIMEOUT_SECONDS", 0.1
        ), patch(
            "orchestrator.workflow_orchestrator.MCP_CONNECTION_TIMEOUT_SECONDS", 0.1
        ), patch(
            "orchestrator.workflow_orchestrator.CUMULATIVE_TIMEOUT_SECONDS", 5.0
        ):
            # Override invoker to use short delays
            class ShortDelayInvoker(SkillInvoker):
                def __init__(self):
                    self.invocations: list[str] = []

                async def invoke(
                    self, skill_name: str, input_data: dict[str, Any], timeout: float
                ) -> SkillResult:
                    self.invocations.append(skill_name)
                    if skill_name == "metric-baseline":
                        # Exceeds the patched 0.1s timeout
                        await asyncio.sleep(1.0)
                    return SkillResult(
                        skill_name=skill_name, success=True, data={}
                    )

            short_invoker = ShortDelayInvoker()
            orchestrator = WorkflowOrchestrator(short_invoker)
            report = await orchestrator.handle_alert(alert)

        # metric-baseline should have timed out
        assert "metric-baseline" in report.unavailable_sources

    @pytest.mark.asyncio
    async def test_cumulative_budget_cancels_remaining_skills(self):
        """When cumulative budget is exceeded, remaining skills are cancelled."""
        # All skills are slow — will exceed budget
        class AllSlowInvoker(SkillInvoker):
            def __init__(self):
                self.invocations: list[str] = []
                self.completed: list[str] = []

            async def invoke(
                self, skill_name: str, input_data: dict[str, Any], timeout: float
            ) -> SkillResult:
                self.invocations.append(skill_name)
                # Every skill takes longer than the budget allows
                await asyncio.sleep(0.5)
                self.completed.append(skill_name)
                return SkillResult(
                    skill_name=skill_name, success=True, data={}
                )

        invoker = AllSlowInvoker()
        alert = _make_alert()

        with patch(
            "orchestrator.workflow_orchestrator.SKILL_TIMEOUT_SECONDS", 2.0
        ), patch(
            "orchestrator.workflow_orchestrator.CUMULATIVE_TIMEOUT_SECONDS", 0.3
        ), patch(
            "orchestrator.workflow_orchestrator.MCP_CONNECTION_TIMEOUT_SECONDS", 2.0
        ):
            orchestrator = WorkflowOrchestrator(invoker)
            start = time.monotonic()
            report = await orchestrator.handle_alert(alert)
            elapsed = time.monotonic() - start

        # Should not take much longer than the budget
        assert elapsed < 3.0  # generous upper bound

        # Report is still produced despite budget exhaustion
        assert report is not None
        assert report.incident_id is not None


# ---------------------------------------------------------------------------
# Test 4: Slack Retry Logic — exponential backoff with simulated failures
# ---------------------------------------------------------------------------


class TestSlackRetryLogic:
    """Test Slack delivery retry with exponential backoff.

    Validates: Requirement 7.5
    """

    @pytest.mark.asyncio
    async def test_slack_retries_on_failure_then_succeeds(self):
        """Slack delivery retries after failures and succeeds on 3rd attempt."""
        slack = MockSlackClient(fail_count=2)  # Fails twice, succeeds on 3rd

        service = SlackDeliveryService(
            slack, channel="#incidents", timeout_budget=30.0
        )

        result = await service.deliver(
            {"text": "Test incident report", "blocks": []},
            incident_id="integ-test-001",
        )

        # Should succeed after retries
        assert result.success is True
        assert result.attempts == 3
        assert result.message_ts == "1709294400.000100"
        assert len(slack.attempts) == 3

    @pytest.mark.asyncio
    async def test_slack_fails_after_max_retries(self):
        """Slack delivery fails after exhausting all 3 retry attempts."""
        slack = MockSlackClient(fail_count=5)  # Always fails

        service = SlackDeliveryService(
            slack, channel="#incidents", timeout_budget=30.0
        )

        result = await service.deliver(
            {"text": "Test incident report", "blocks": []},
            incident_id="integ-test-002",
        )

        # Should fail after max attempts
        assert result.success is False
        assert result.attempts == 3  # max attempts
        assert result.local_path is not None  # stored locally
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_slack_retry_in_full_pipeline(self):
        """Full pipeline retries Slack delivery on transient failures."""
        invoker = RealisticSkillInvoker()
        slack = MockSlackClient(fail_count=1)  # Fails once, succeeds on 2nd

        result = await run_pipeline(
            raw_payload={"source": "cloudwatch", "payload": {}},
            skill_invoker=invoker,
            slack_client=slack,
            slack_channel="#incidents",
        )

        # Pipeline succeeds despite first Slack failure
        assert result.success is True
        assert result.delivery_result is not None
        assert result.delivery_result.success is True
        assert result.delivery_result.attempts == 2


# ---------------------------------------------------------------------------
# Test 5: Interactive Triage — session persistence and follow-ups
# ---------------------------------------------------------------------------


class TestInteractiveTriageSession:
    """Test interactive triage session across multiple follow-ups.

    Validates: Requirements 12.5 (via session context), 11.1
    """

    def test_start_investigation_creates_session(self):
        """start_investigation creates a session with correct initial state."""
        session, annotations, skills = start_investigation(
            "our prod-cluster is throwing 503 errors in us-east-1"
        )

        assert session.session_id is not None
        assert session.normalized_alert is not None
        assert session.normalized_alert.region == "us-east-1"
        assert session.evidence_accumulator == []
        assert len(session.conversation_history) == 1
        assert session.conversation_history[0].role == "user"
        assert len(skills) >= 1  # At least one skill triggered

    def test_follow_up_accumulates_evidence(self):
        """Follow-up queries accumulate evidence without discarding prior data."""
        # Register a mock skill
        call_count = {"value": 0}

        def mock_log_triage(params: dict) -> dict:
            call_count["value"] += 1
            return {
                "evidence_items": [
                    {
                        "claim": f"Found error pattern #{call_count['value']}",
                        "source": "log-triage",
                        "weight": 0.6,
                    }
                ]
            }

        def mock_metric_baseline(params: dict) -> dict:
            return {
                "evidence_items": [
                    {
                        "claim": "CPU deviation critical",
                        "source": "metric-baseline",
                        "weight": 0.8,
                    }
                ]
            }

        register_skill("log-triage", mock_log_triage)
        register_skill("metric-baseline", mock_metric_baseline)

        # Start investigation
        session, _, _ = start_investigation("high latency in payment-service")

        # First follow-up
        response1 = follow_up(session.session_id, "check the logs for errors")
        evidence_after_first = len(session.evidence_accumulator)
        assert evidence_after_first > 0
        assert len(response1.new_evidence) > 0

        # Second follow-up
        response2 = follow_up(session.session_id, "check logs again for timeouts")
        evidence_after_second = len(session.evidence_accumulator)

        # Evidence only accumulates — monotonically non-decreasing
        assert evidence_after_second >= evidence_after_first

        # Conversation history grows
        # Initial user + (user + agent) per follow-up
        assert len(session.conversation_history) >= 5

    def test_follow_up_preserves_prior_context(self):
        """Each follow-up has access to full prior conversation context."""
        def mock_skill(params: dict) -> dict:
            return {"evidence_items": [{"claim": "test", "source": "mock", "weight": 0.5}]}

        register_skill("log-triage", mock_skill)
        register_skill("metric-baseline", mock_skill)

        session, _, _ = start_investigation("errors in prod")

        # Do several follow-ups
        follow_up(session.session_id, "check logs")
        follow_up(session.session_id, "check metrics")
        follow_up(session.session_id, "what's your best guess?")

        # All turns are preserved
        # 1 initial + 3 follow-ups × 2 turns (user+agent) = 7
        assert len(session.conversation_history) >= 7

        # All user prompts are in history
        user_contents = [
            t.content for t in session.conversation_history if t.role == "user"
        ]
        assert "errors in prod" in user_contents
        assert "check logs" in user_contents
        assert "check metrics" in user_contents


    def test_direct_skill_invocation_in_session(self):
        """Direct skill invocation adds evidence to session accumulator."""
        def mock_node_check(params: dict) -> dict:
            return {
                "evidence_items": [
                    {"claim": "Node ip-10-0-1-5 has MemoryPressure", "source": "node-condition-check", "weight": 0.7},
                    {"claim": "Node ip-10-0-2-3 is NotReady", "source": "node-condition-check", "weight": 0.9},
                ]
            }

        register_skill("node-condition-check", mock_node_check)
        register_skill("log-triage", lambda p: {"evidence_items": []})
        register_skill("metric-baseline", lambda p: {"evidence_items": []})

        session, _, _ = start_investigation("nodes unhealthy in prod-cluster")

        # Direct invocation bypassing orchestrator workflow
        response = invoke_skill(
            session.session_id,
            "node-condition-check",
            {"cluster": "prod-cluster"},
        )

        assert len(response.new_evidence) == 2
        assert len(session.evidence_accumulator) == 2
        assert any("MemoryPressure" in e.claim for e in session.evidence_accumulator)

    def test_skill_failure_in_session_is_graceful(self):
        """Failed skill invocation reports error but session continues."""
        def failing_skill(params: dict) -> dict:
            raise ConnectionError("MCP connection refused")

        register_skill("k8s-cluster-health", failing_skill)
        register_skill("log-triage", lambda p: {"evidence_items": [{"claim": "ok", "source": "log-triage", "weight": 0.5}]})
        register_skill("metric-baseline", lambda p: {"evidence_items": []})

        session, _, _ = start_investigation("cluster issues")

        # Direct invocation of failing skill
        response = invoke_skill(session.session_id, "k8s-cluster-health", {})

        # Failure is reported but session is intact
        assert "failed" in response.summary.lower()
        assert len(response.suggested_next_steps) >= 1

        # Session can continue with other skills
        response2 = follow_up(session.session_id, "check the logs instead")
        assert session.session_id is not None  # session still active

    @pytest.mark.asyncio
    async def test_triage_with_full_workflow_integration(self):
        """Interactive triage can trigger the full orchestrator workflow."""
        invoker = RealisticSkillInvoker()
        slack = MockSlackClient()

        session, pipeline_result = await triage_with_full_workflow(
            prompt="503 errors on prod-cluster in us-east-1",
            skill_invoker=invoker,
            slack_client=slack,
            slack_channel="#incidents",
        )

        # Session created with synthesized alert
        assert session.normalized_alert is not None
        assert session.normalized_alert.region == "us-east-1"

        # Full pipeline ran
        assert pipeline_result.success is True
        assert pipeline_result.incident_report is not None
        assert pipeline_result.delivery_result is not None
        assert pipeline_result.delivery_result.success is True

        # All core signal skills were invoked
        invoked = {name for name, _ in invoker.invocations}
        assert "metric-baseline" in invoked
        assert "deploy-correlation" in invoked
        assert "log-triage" in invoked
