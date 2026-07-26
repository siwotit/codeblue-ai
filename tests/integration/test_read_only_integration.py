"""Integration tests for read-only enforcement across full workflow execution.

Task 18.3: Verify that no write operations are invoked across the full
pipeline execution, that rejected operations are logged, and that only
slack.postMessage is permitted as a write.

Requirements: 10.1, 10.2, 10.5, 10.6
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from orchestrator.pipeline import run_pipeline
from orchestrator.read_only_guard import (
    ALLOWED_OPERATIONS,
    READ_ONLY_MCPS,
    ReadOnlyGuard,
    ReadOnlyViolationError,
    RejectedInvocation,
    SLACK_ALLOWED_WRITES,
)
from orchestrator.tool_registry import (
    MCP_OPERATION_ALLOWLIST,
    MCP_OPERATION_DENYLIST,
    WriteOperationError,
    clear_rejected_operations,
    get_rejected_operations,
    validate_operation,
)
from orchestrator.workflow_orchestrator import (
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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_rejection_log():
    """Clear the global rejection log before and after each test."""
    clear_rejected_operations()
    yield
    clear_rejected_operations()


def _make_alert(**overrides: Any) -> NormalizedAlert:
    """Create a NormalizedAlert for testing."""
    defaults = {
        "id": "readonly-test-001",
        "source": AlertSource.CLOUDWATCH,
        "source_alarm_id": "arn:aws:cloudwatch:us-east-1:123456789012:alarm:CPUAlarm",
        "severity": Severity.HIGH,
        "title": "High CPU utilization on prod-api",
        "description": "CPU exceeded 90% threshold",
        "state": AlertState.FIRING,
        "fired_at": datetime(2024, 3, 10, 14, 0, 0, tzinfo=timezone.utc),
        "affected_resources": [
            ResourceIdentifier(
                type=ResourceIdentifierType.ARN,
                value="arn:aws:ec2:us-east-1:123456789012:instance/i-abc123",
                displayName="prod-api-instance",
            )
        ],
        "region": "us-east-1",
        "raw_payload": {"AlarmName": "CPUAlarm", "NewStateValue": "ALARM"},
    }
    defaults.update(overrides)
    return NormalizedAlert(**defaults)


# ---------------------------------------------------------------------------
# Mock Skill Invoker that tracks MCP operations
# ---------------------------------------------------------------------------


class ReadOnlyTrackingInvoker(SkillInvoker):
    """Skill invoker that simulates all skill responses and tracks operations.

    This mock returns realistic skill responses while recording which
    MCP operations would be invoked. It wraps a ReadOnlyGuard to validate
    every operation.
    """

    def __init__(self, guard: ReadOnlyGuard) -> None:
        self.guard = guard
        self.invocations: list[str] = []
        self.mcp_operations_invoked: list[str] = []

    async def invoke(
        self, skill_name: str, input_data: dict[str, Any], timeout: float
    ) -> SkillResult:
        self.invocations.append(skill_name)

        # Simulate the MCP operations that each skill would invoke
        ops = self._get_skill_mcp_operations(skill_name)
        for op in ops:
            self.mcp_operations_invoked.append(op)
            # Validate each operation through the guard
            self.guard.validate_invocation(op)

        # Return simulated successful responses
        return SkillResult(
            skill_name=skill_name,
            success=True,
            data=self._get_mock_response(skill_name),
        )

    def _get_skill_mcp_operations(self, skill_name: str) -> list[str]:
        """Return the MCP operations a skill would invoke in production."""
        ops_map: dict[str, list[str]] = {
            "alert-ingestion": [
                "cloudwatch.describeAlarms",
            ],
            "metric-baseline": [
                "cloudwatch.getMetricData",
                "cloudwatch.getMetricStatistics",
                "cloudwatch.listMetrics",
            ],
            "deploy-correlation": [
                "aws.lookupCloudTrailEvents",
                "kubernetes.getDeployments",
                "kubernetes.getReplicaSets",
            ],
            "log-triage": [
                "cloudwatch.queryLogInsights",
                "cloudwatch.describeLogGroups",
                "cloudwatch.filterLogEvents",
            ],
            "k8s-cluster-health": [
                "kubernetes.getNodes",
                "kubernetes.getPods",
                "kubernetes.getEvents",
            ],
            "pod-failure-triage": [
                "kubernetes.getPods",
                "kubernetes.getEvents",
            ],
            "node-condition-check": [
                "kubernetes.getNodes",
                "kubernetes.describeNode",
            ],
            "eks-addon-status": [
                "kubernetes.getAddonStatus",
            ],
            "hypothesis-engine": [],  # Reasoning only, no MCP calls
            "escalation-decision": [],  # Reasoning only
            "evidence-provenance": [
                "cloudwatch.getMetricData",
                "aws.lookupCloudTrailEvents",
            ],
            "incident-summary-format": [
                "slack.postMessage",
            ],
        }
        return ops_map.get(skill_name, [])

    def _get_mock_response(self, skill_name: str) -> dict[str, Any]:
        """Return a mock response for the skill."""
        if skill_name == "hypothesis-engine":
            return {
                "hypotheses": [
                    {
                        "rank": 1,
                        "title": "Deployment caused CPU spike",
                        "confidence": 0.75,
                        "supportingEvidence": [],
                        "suggestedVerification": ["Check rollout history"],
                    }
                ]
            }
        return {}


class RogueSkillInvoker(SkillInvoker):
    """Skill invoker that simulates a 'rogue' skill attempting write operations.

    This invoker will attempt to invoke write operations on monitored MCPs,
    which should be caught and rejected by the ReadOnlyGuard.
    """

    def __init__(self, guard: ReadOnlyGuard) -> None:
        self.guard = guard
        self.invocations: list[str] = []
        self.rejected_ops: list[str] = []
        self.allowed_ops: list[str] = []

    async def invoke(
        self, skill_name: str, input_data: dict[str, Any], timeout: float
    ) -> SkillResult:
        self.invocations.append(skill_name)

        # Simulate normal read operations first
        read_ops = ["cloudwatch.getMetricData", "kubernetes.getPods"]
        for op in read_ops:
            self.guard.validate_invocation(op)
            self.allowed_ops.append(op)

        # Now attempt write operations (simulating a rogue skill)
        write_attempts = [
            "kubernetes.apply",
            "cloudwatch.putMetricAlarm",
            "aws.terminateInstances",
            "grafana.deleteDashboard",
        ]
        for op in write_attempts:
            try:
                self.guard.validate_invocation(op)
                self.allowed_ops.append(op)
            except ReadOnlyViolationError:
                self.rejected_ops.append(op)

        return SkillResult(skill_name=skill_name, success=True, data={})


# ---------------------------------------------------------------------------
# Test: No write operations in full workflow execution
# ---------------------------------------------------------------------------


class TestNoWriteOperationsInWorkflow:
    """Verify that no write operations are invoked across a full workflow.

    **Validates: Requirements 10.1, 10.2, 10.5**
    """

    @pytest.mark.asyncio
    async def test_full_workflow_invokes_zero_writes_on_monitored_mcps(self):
        """Running the full orchestrator workflow produces zero write ops
        on cloudwatch, grafana, kubernetes, and aws MCPs."""
        guard = ReadOnlyGuard()
        invoker = ReadOnlyTrackingInvoker(guard)
        orchestrator = WorkflowOrchestrator(invoker)
        alert = _make_alert()

        # Execute the full workflow
        report = await orchestrator.handle_alert(alert)

        # Verify report was produced
        assert report is not None
        assert report.incident_id is not None

        # Verify ZERO rejections — all operations were read-only
        assert guard.rejection_count == 0, (
            f"Expected 0 rejections but found {guard.rejection_count}: "
            f"{[r.operation for r in guard.get_rejection_log()]}"
        )

        # Verify all MCP operations invoked are in the allowed set
        for op in invoker.mcp_operations_invoked:
            mcp = op.split(".")[0]
            if mcp in READ_ONLY_MCPS:
                assert op in ALLOWED_OPERATIONS, (
                    f"Operation '{op}' on read-only MCP '{mcp}' is not in allowlist"
                )

    @pytest.mark.asyncio
    async def test_full_workflow_with_k8s_cluster_alert(self):
        """Full workflow with EKS cluster context still produces zero writes."""
        guard = ReadOnlyGuard()
        invoker = ReadOnlyTrackingInvoker(guard)
        orchestrator = WorkflowOrchestrator(invoker)
        alert = _make_alert(cluster="prod-eks-cluster", namespace="payments")

        report = await orchestrator.handle_alert(alert)

        assert report is not None
        assert guard.rejection_count == 0

        # K8s skills should have been invoked
        assert "k8s-cluster-health" in invoker.invocations
        assert "pod-failure-triage" in invoker.invocations
        assert "node-condition-check" in invoker.invocations
        assert "eks-addon-status" in invoker.invocations

        # All K8s ops should be read-only
        k8s_ops = [
            op for op in invoker.mcp_operations_invoked
            if op.startswith("kubernetes.")
        ]
        assert len(k8s_ops) > 0, "Expected K8s operations to be invoked"
        for op in k8s_ops:
            assert op in ALLOWED_OPERATIONS, (
                f"K8s operation '{op}' is not in the read-only allowlist"
            )

    @pytest.mark.asyncio
    async def test_full_pipeline_end_to_end_no_writes(self):
        """The full pipeline (ingestion → workflow → format) invokes no writes
        except the permitted slack.postMessage."""
        guard = ReadOnlyGuard()
        invoker = ReadOnlyTrackingInvoker(guard)
        alert = _make_alert()
        alert_data = alert.model_dump(by_alias=True, mode="json")

        # Override the alert-ingestion response to return a valid alert
        original_invoke = invoker.invoke

        async def patched_invoke(
            skill_name: str, input_data: dict[str, Any], timeout: float
        ) -> SkillResult:
            if skill_name == "alert-ingestion":
                invoker.invocations.append(skill_name)
                return SkillResult(
                    skill_name="alert-ingestion",
                    success=True,
                    data={"result": alert_data},
                )
            return await original_invoke(skill_name, input_data, timeout)

        invoker.invoke = patched_invoke

        result = await run_pipeline(
            raw_payload={"source": "cloudwatch", "payload": {"AlarmName": "Test"}},
            skill_invoker=invoker,
        )

        assert result.success is True

        # The only write-like operation should be slack.postMessage
        write_ops = [
            op for op in invoker.mcp_operations_invoked
            if op.startswith("slack.")
        ]
        for op in write_ops:
            assert op in SLACK_ALLOWED_WRITES, (
                f"Slack operation '{op}' is not in the permitted writes set"
            )

        # No rejections should have occurred
        assert guard.rejection_count == 0


# ---------------------------------------------------------------------------
# Test: Rejected operations are logged
# ---------------------------------------------------------------------------


class TestRejectedOperationsLogged:
    """Verify that attempted write operations are rejected and logged.

    **Validates: Requirements 10.2, 10.6**
    """

    @pytest.mark.asyncio
    async def test_rogue_skill_writes_are_caught_and_logged(self):
        """When a skill attempts write operations, they are rejected and
        the rejection log is populated."""
        guard = ReadOnlyGuard()
        invoker = RogueSkillInvoker(guard)

        # Invoke the rogue skill
        await invoker.invoke("rogue-skill", {}, timeout=10.0)

        # All write attempts should have been rejected
        assert len(invoker.rejected_ops) == 4
        assert "kubernetes.apply" in invoker.rejected_ops
        assert "cloudwatch.putMetricAlarm" in invoker.rejected_ops
        assert "aws.terminateInstances" in invoker.rejected_ops
        assert "grafana.deleteDashboard" in invoker.rejected_ops

        # Read operations should have been allowed
        assert "cloudwatch.getMetricData" in invoker.allowed_ops
        assert "kubernetes.getPods" in invoker.allowed_ops

        # The guard's rejection log should be populated
        rejection_log = guard.get_rejection_log()
        assert len(rejection_log) == 4
        rejected_operations = {r.operation for r in rejection_log}
        assert "kubernetes.apply" in rejected_operations
        assert "cloudwatch.putMetricAlarm" in rejected_operations
        assert "aws.terminateInstances" in rejected_operations
        assert "grafana.deleteDashboard" in rejected_operations

    @pytest.mark.asyncio
    async def test_rejection_log_contains_timestamps_and_reasons(self):
        """Each rejection record includes a timestamp and reason."""
        guard = ReadOnlyGuard()

        # Attempt a write operation
        with pytest.raises(ReadOnlyViolationError):
            guard.validate_invocation("kubernetes.delete")

        log = guard.get_rejection_log()
        assert len(log) == 1
        record = log[0]
        assert record.operation == "kubernetes.delete"
        assert record.mcp == "kubernetes"
        assert record.timestamp  # non-empty ISO 8601
        assert record.reason  # non-empty reason string

    @pytest.mark.asyncio
    async def test_tool_registry_rejection_log_populated(self):
        """The tool_registry module's global rejection log tracks write attempts."""
        # Attempt a denied operation via the tool_registry
        with pytest.raises(WriteOperationError):
            validate_operation("kubernetes.apply")

        rejected = get_rejected_operations()
        assert len(rejected) >= 1
        assert any(r.operation == "kubernetes.apply" for r in rejected)

    @pytest.mark.asyncio
    async def test_multiple_write_attempts_all_logged(self):
        """Multiple write attempts across different MCPs are all individually logged."""
        guard = ReadOnlyGuard()
        write_ops = [
            "cloudwatch.putMetricData",
            "grafana.createAlertRule",
            "kubernetes.scale",
            "aws.runInstances",
        ]

        for op in write_ops:
            with pytest.raises(ReadOnlyViolationError):
                guard.validate_invocation(op)

        log = guard.get_rejection_log()
        assert len(log) == len(write_ops)
        logged_ops = [r.operation for r in log]
        for op in write_ops:
            assert op in logged_ops


# ---------------------------------------------------------------------------
# Test: Only slack.postMessage is permitted as a write
# ---------------------------------------------------------------------------


class TestSlackWritePermissions:
    """Verify that only Slack MCP writes (postMessage, updateMessage)
    are permitted.

    **Validates: Requirements 10.1, 10.5**
    """

    def test_slack_post_message_is_allowed(self):
        """slack.postMessage passes validation without rejection."""
        guard = ReadOnlyGuard()
        result = guard.validate_invocation("slack.postMessage")
        assert result is True
        assert guard.rejection_count == 0

    def test_slack_update_message_is_allowed(self):
        """slack.updateMessage passes validation without rejection."""
        guard = ReadOnlyGuard()
        result = guard.validate_invocation("slack.updateMessage")
        assert result is True
        assert guard.rejection_count == 0

    def test_other_slack_writes_are_rejected(self):
        """Non-permitted Slack write operations are rejected."""
        guard = ReadOnlyGuard()

        # deleteMessage is not in the allowed Slack writes
        with pytest.raises(ReadOnlyViolationError):
            guard.validate_invocation("slack.deleteMessage")

        assert guard.rejection_count == 1

    def test_monitored_mcp_writes_always_rejected(self):
        """Write operations on monitored MCPs (cloudwatch, grafana,
        kubernetes, aws) are always rejected."""
        guard = ReadOnlyGuard()

        monitored_writes = [
            "cloudwatch.putMetricAlarm",
            "cloudwatch.deleteAlarms",
            "grafana.createDashboard",
            "grafana.deleteAlertRule",
            "kubernetes.apply",
            "kubernetes.delete",
            "aws.terminateInstances",
            "aws.createStack",
        ]

        for op in monitored_writes:
            with pytest.raises(ReadOnlyViolationError):
                guard.validate_invocation(op)

        assert guard.rejection_count == len(monitored_writes)


# ---------------------------------------------------------------------------
# Test: Rogue skill scenario — full workflow with attempted writes
# ---------------------------------------------------------------------------


class TestRogueSkillWorkflowScenario:
    """Test the scenario where a 'rogue' skill attempts writes during workflow.

    **Validates: Requirements 10.5, 10.6**
    """

    @pytest.mark.asyncio
    async def test_rogue_skill_in_workflow_writes_caught(self):
        """A rogue skill's write attempts are caught without breaking workflow."""
        guard = ReadOnlyGuard()
        captured_rejections: list[str] = []

        class WorkflowWithRogueSkill(SkillInvoker):
            """Simulates a workflow where one skill attempts unauthorized writes."""

            async def invoke(
                self, skill_name: str, input_data: dict[str, Any], timeout: float
            ) -> SkillResult:
                # Normal skills perform read operations
                if skill_name in ("metric-baseline", "log-triage", "deploy-correlation"):
                    guard.validate_invocation(f"cloudwatch.getMetricData")
                    return SkillResult(skill_name=skill_name, success=True, data={})

                # The "rogue" hypothesis-engine tries to write
                if skill_name == "hypothesis-engine":
                    # First do legitimate reads
                    guard.validate_invocation("cloudwatch.getMetricData")

                    # Then attempt unauthorized writes
                    rogue_writes = [
                        "kubernetes.scale",
                        "aws.updateStack",
                    ]
                    for op in rogue_writes:
                        try:
                            guard.validate_invocation(op)
                        except ReadOnlyViolationError:
                            captured_rejections.append(op)

                    return SkillResult(
                        skill_name=skill_name,
                        success=True,
                        data={"hypotheses": []},
                    )

                return SkillResult(skill_name=skill_name, success=True, data={})

        invoker = WorkflowWithRogueSkill()
        orchestrator = WorkflowOrchestrator(invoker)
        alert = _make_alert()

        # The workflow should complete despite the rogue writes being rejected
        report = await orchestrator.handle_alert(alert)

        assert report is not None
        assert report.incident_id is not None

        # The rogue writes should have been caught
        assert len(captured_rejections) == 2
        assert "kubernetes.scale" in captured_rejections
        assert "aws.updateStack" in captured_rejections

        # Guard rejection log should confirm this
        log = guard.get_rejection_log()
        assert len(log) == 2
        assert all(r.mcp in READ_ONLY_MCPS for r in log)

    @pytest.mark.asyncio
    async def test_unknown_mcp_operations_rejected(self):
        """Operations on unknown MCPs are rejected (fail closed)."""
        guard = ReadOnlyGuard()

        with pytest.raises(ReadOnlyViolationError) as exc_info:
            guard.validate_invocation("unknown_mcp.doSomething")

        assert "unknown MCP" in exc_info.value.reason
        assert guard.rejection_count == 1

    @pytest.mark.asyncio
    async def test_ambiguous_operations_on_readonly_mcps_rejected(self):
        """Ambiguous operations on read-only MCPs are rejected (fail closed)."""
        guard = ReadOnlyGuard()

        # "execute" is not clearly a read or write verb
        with pytest.raises(ReadOnlyViolationError):
            guard.validate_invocation("kubernetes.executeCommand")

        # The guard should fail closed for ambiguous verbs
        assert guard.rejection_count == 1


# ---------------------------------------------------------------------------
# Test: ReadOnlyGuard integration with validate_operation
# ---------------------------------------------------------------------------


class TestReadOnlyGuardConsistency:
    """Verify ReadOnlyGuard and tool_registry are consistent.

    **Validates: Requirements 10.1, 10.2**
    """

    def test_all_allowlisted_ops_pass_guard(self):
        """Every operation in the allowlist passes the ReadOnlyGuard."""
        guard = ReadOnlyGuard()

        for op in ALLOWED_OPERATIONS:
            result = guard.validate_invocation(op)
            assert result is True, f"Allowlisted operation '{op}' was rejected"

        assert guard.rejection_count == 0

    def test_all_denylisted_ops_fail_guard(self):
        """Every operation in the denylist is rejected by the ReadOnlyGuard."""
        guard = ReadOnlyGuard()

        from orchestrator.read_only_guard import DENIED_OPERATIONS

        for op in DENIED_OPERATIONS:
            with pytest.raises(ReadOnlyViolationError):
                guard.validate_invocation(op)

        assert guard.rejection_count == len(DENIED_OPERATIONS)

    def test_tool_registry_allowlist_subset_of_guard_allowlist(self):
        """tool_registry's MCP_OPERATION_ALLOWLIST is a subset of
        read_only_guard's ALLOWED_OPERATIONS."""
        for op in MCP_OPERATION_ALLOWLIST:
            assert op in ALLOWED_OPERATIONS or op.startswith("slack."), (
                f"tool_registry allows '{op}' but read_only_guard does not"
            )

    def test_tool_registry_denylist_subset_of_guard_denylist(self):
        """All operations in tool_registry's denylist are also denied by the guard."""
        guard = ReadOnlyGuard()

        for op in MCP_OPERATION_DENYLIST:
            assert not guard.is_allowed(op), (
                f"Operation '{op}' is in tool_registry denylist but passes the guard"
            )
