"""Tests for the CodeBlue AI Read-Only Enforcement Guard.

Validates that the read-only guard correctly:
- Permits read/describe/list/get operations on all monitored MCPs
- Rejects write/create/update/delete operations on monitored systems
- Permits only Slack MCP write operations (postMessage, updateMessage)
- Logs all rejected operations for audit
- Fails closed on unknown operations

Requirements: 10.1, 10.2, 10.3, 10.5, 10.6
"""

import pytest

from orchestrator.read_only_guard import (
    ALLOWED_OPERATIONS,
    DENIED_OPERATIONS,
    READ_ONLY_MCPS,
    READ_VERB_PREFIXES,
    SLACK_ALLOWED_WRITES,
    WRITE_PERMITTED_MCPS,
    WRITE_VERB_PREFIXES,
    ReadOnlyGuard,
    ReadOnlyViolationError,
    RejectedInvocation,
)


@pytest.fixture
def guard() -> ReadOnlyGuard:
    """Create a fresh ReadOnlyGuard instance for each test."""
    return ReadOnlyGuard()


class TestAllowlistIntegrity:
    """Tests that the allowlist and denylist are correctly structured."""

    def test_allowlist_and_denylist_do_not_overlap(self):
        overlap = ALLOWED_OPERATIONS & DENIED_OPERATIONS
        assert len(overlap) == 0, f"Overlap found: {overlap}"

    def test_only_slack_in_write_permitted_mcps(self):
        assert WRITE_PERMITTED_MCPS == frozenset({"slack"})

    def test_all_monitored_mcps_are_read_only(self):
        expected = {"cloudwatch", "grafana", "kubernetes", "aws"}
        assert READ_ONLY_MCPS == frozenset(expected)

    def test_slack_allowed_writes_are_messaging_only(self):
        assert SLACK_ALLOWED_WRITES == frozenset({
            "slack.postMessage",
            "slack.updateMessage",
        })

    def test_slack_writes_are_in_allowlist(self):
        for op in SLACK_ALLOWED_WRITES:
            assert op in ALLOWED_OPERATIONS

    def test_no_write_operations_in_allowlist_for_monitored_mcps(self):
        """Verify no write verbs appear in allowlist for read-only MCPs."""
        for op in ALLOWED_OPERATIONS:
            mcp = op.split(".")[0]
            if mcp in READ_ONLY_MCPS:
                action = op.split(".", 1)[1].lower()
                for prefix in WRITE_VERB_PREFIXES:
                    assert not action.startswith(prefix), (
                        f"Write verb '{prefix}' found in allowlist for read-only "
                        f"MCP '{mcp}': {op}"
                    )


class TestReadOperationsAllowed:
    """Requirement 10.1: Expose only read/describe/list/get operations."""

    def test_cloudwatch_read_operations(self, guard: ReadOnlyGuard):
        ops = [
            "cloudwatch.describeAlarms",
            "cloudwatch.getMetricData",
            "cloudwatch.getMetricStatistics",
            "cloudwatch.listMetrics",
            "cloudwatch.queryLogInsights",
            "cloudwatch.describeLogGroups",
            "cloudwatch.getLogEvents",
            "cloudwatch.filterLogEvents",
        ]
        for op in ops:
            assert guard.validate_invocation(op) is True

    def test_grafana_read_operations(self, guard: ReadOnlyGuard):
        ops = [
            "grafana.getAlertRules",
            "grafana.getDashboards",
            "grafana.getDashboardPanels",
            "grafana.queryMetrics",
            "grafana.getAnnotations",
        ]
        for op in ops:
            assert guard.validate_invocation(op) is True

    def test_kubernetes_read_operations(self, guard: ReadOnlyGuard):
        ops = [
            "kubernetes.getPods",
            "kubernetes.getNodes",
            "kubernetes.getEvents",
            "kubernetes.getDeployments",
            "kubernetes.getReplicaSets",
            "kubernetes.describeNode",
            "kubernetes.describePod",
            "kubernetes.getAddonStatus",
            "kubernetes.getLogs",
        ]
        for op in ops:
            assert guard.validate_invocation(op) is True

    def test_aws_read_operations(self, guard: ReadOnlyGuard):
        ops = [
            "aws.lookupCloudTrailEvents",
            "aws.describeEksCluster",
            "aws.listEksClusters",
            "aws.describeInstances",
            "aws.describeAutoScalingGroups",
        ]
        for op in ops:
            assert guard.validate_invocation(op) is True


class TestWriteOperationsRejected:
    """Requirement 10.2: Do NOT expose create/update/delete/mutate operations."""

    def test_kubernetes_write_operations_rejected(self, guard: ReadOnlyGuard):
        ops = [
            "kubernetes.apply",
            "kubernetes.delete",
            "kubernetes.patch",
            "kubernetes.scale",
            "kubernetes.rolloutRestart",
            "kubernetes.rolloutUndo",
            "kubernetes.createNamespace",
            "kubernetes.deleteNamespace",
            "kubernetes.cordonNode",
            "kubernetes.drainNode",
            "kubernetes.taint",
            "kubernetes.exec",
        ]
        for op in ops:
            with pytest.raises(ReadOnlyViolationError) as exc_info:
                guard.validate_invocation(op)
            assert exc_info.value.mcp == "kubernetes"

    def test_cloudwatch_write_operations_rejected(self, guard: ReadOnlyGuard):
        ops = [
            "cloudwatch.putMetricAlarm",
            "cloudwatch.deleteAlarms",
            "cloudwatch.setAlarmState",
            "cloudwatch.putMetricData",
            "cloudwatch.createLogGroup",
            "cloudwatch.deleteLogGroup",
        ]
        for op in ops:
            with pytest.raises(ReadOnlyViolationError):
                guard.validate_invocation(op)

    def test_grafana_write_operations_rejected(self, guard: ReadOnlyGuard):
        ops = [
            "grafana.createAlertRule",
            "grafana.updateAlertRule",
            "grafana.deleteAlertRule",
            "grafana.createDashboard",
            "grafana.updateDashboard",
            "grafana.deleteDashboard",
        ]
        for op in ops:
            with pytest.raises(ReadOnlyViolationError):
                guard.validate_invocation(op)

    def test_aws_write_operations_rejected(self, guard: ReadOnlyGuard):
        ops = [
            "aws.createStack",
            "aws.updateStack",
            "aws.deleteStack",
            "aws.runInstances",
            "aws.terminateInstances",
            "aws.stopInstances",
            "aws.rebootInstances",
            "aws.createCluster",
            "aws.deleteCluster",
        ]
        for op in ops:
            with pytest.raises(ReadOnlyViolationError):
                guard.validate_invocation(op)

    def test_unknown_write_verbs_on_monitored_mcps_rejected(self, guard: ReadOnlyGuard):
        """Even operations not in the explicit denylist are rejected if write-like."""
        ops = [
            "kubernetes.createCustomResource",
            "aws.modifyInstanceAttribute",
            "cloudwatch.putDashboard",
            "grafana.updateUser",
        ]
        for op in ops:
            with pytest.raises(ReadOnlyViolationError):
                guard.validate_invocation(op)


class TestSlackWritePermissions:
    """Requirement 10.3: Only Slack MCP may perform write operations."""

    def test_slack_post_message_allowed(self, guard: ReadOnlyGuard):
        assert guard.validate_invocation("slack.postMessage") is True

    def test_slack_update_message_allowed(self, guard: ReadOnlyGuard):
        assert guard.validate_invocation("slack.updateMessage") is True

    def test_slack_read_operations_allowed(self, guard: ReadOnlyGuard):
        """Slack read operations should also be fine."""
        assert guard.validate_invocation("slack.getChannels") is True
        assert guard.validate_invocation("slack.listUsers") is True

    def test_slack_destructive_writes_rejected(self, guard: ReadOnlyGuard):
        """Slack write operations beyond messaging are rejected."""
        with pytest.raises(ReadOnlyViolationError):
            guard.validate_invocation("slack.deleteChannel")
        with pytest.raises(ReadOnlyViolationError):
            guard.validate_invocation("slack.kickUser")


class TestFailClosedBehavior:
    """The guard must fail closed — reject anything it can't positively identify as safe."""

    def test_unknown_mcp_rejected(self, guard: ReadOnlyGuard):
        """Operations on unknown MCPs are rejected."""
        with pytest.raises(ReadOnlyViolationError) as exc_info:
            guard.validate_invocation("unknown_mcp.getStuff")
        assert "unknown MCP" in exc_info.value.reason

    def test_malformed_operation_rejected(self, guard: ReadOnlyGuard):
        """Operations without MCP prefix are rejected."""
        with pytest.raises(ReadOnlyViolationError):
            guard.validate_invocation("noDotSeparator")

    def test_ambiguous_verb_on_read_only_mcp_rejected(self, guard: ReadOnlyGuard):
        """Operations with ambiguous verbs on read-only MCPs are rejected."""
        with pytest.raises(ReadOnlyViolationError) as exc_info:
            guard.validate_invocation("kubernetes.doSomething")
        assert "fail closed" in exc_info.value.reason


class TestRejectionAuditLog:
    """Requirement 10.6: Reject and log attempted write operations."""

    def test_rejected_operation_is_logged(self, guard: ReadOnlyGuard):
        with pytest.raises(ReadOnlyViolationError):
            guard.validate_invocation("kubernetes.delete")
        log = guard.get_rejection_log()
        assert len(log) == 1
        assert log[0].operation == "kubernetes.delete"
        assert log[0].mcp == "kubernetes"

    def test_multiple_rejections_accumulate(self, guard: ReadOnlyGuard):
        for op in ["kubernetes.delete", "aws.terminateInstances", "cloudwatch.putMetricAlarm"]:
            with pytest.raises(ReadOnlyViolationError):
                guard.validate_invocation(op)
        assert guard.rejection_count == 3

    def test_rejected_operation_has_timestamp(self, guard: ReadOnlyGuard):
        with pytest.raises(ReadOnlyViolationError):
            guard.validate_invocation("kubernetes.apply")
        log = guard.get_rejection_log()
        # ISO 8601 format check
        assert "T" in log[0].timestamp
        assert "+" in log[0].timestamp or "Z" in log[0].timestamp

    def test_rejected_operation_has_reason(self, guard: ReadOnlyGuard):
        with pytest.raises(ReadOnlyViolationError):
            guard.validate_invocation("kubernetes.delete")
        log = guard.get_rejection_log()
        assert log[0].reason != ""

    def test_allowed_operations_are_not_logged(self, guard: ReadOnlyGuard):
        guard.validate_invocation("kubernetes.getPods")
        guard.validate_invocation("cloudwatch.getMetricData")
        guard.validate_invocation("slack.postMessage")
        assert guard.rejection_count == 0

    def test_context_is_recorded(self, guard: ReadOnlyGuard):
        ctx = {"incident_id": "INC-001", "phase": "signal_collection"}
        with pytest.raises(ReadOnlyViolationError):
            guard.validate_invocation("kubernetes.delete", context=ctx)
        log = guard.get_rejection_log()
        assert log[0].context == ctx

    def test_clear_rejection_log(self, guard: ReadOnlyGuard):
        with pytest.raises(ReadOnlyViolationError):
            guard.validate_invocation("kubernetes.delete")
        assert guard.rejection_count == 1
        guard.clear_rejection_log()
        assert guard.rejection_count == 0

    def test_rejected_invocation_is_frozen(self, guard: ReadOnlyGuard):
        with pytest.raises(ReadOnlyViolationError):
            guard.validate_invocation("kubernetes.delete")
        record = guard.get_rejection_log()[0]
        assert isinstance(record, RejectedInvocation)
        with pytest.raises(Exception):
            record.operation = "modified"  # type: ignore[misc]


class TestIsAllowedConvenience:
    """Tests for the is_allowed() convenience method."""

    def test_returns_true_for_read_operations(self, guard: ReadOnlyGuard):
        assert guard.is_allowed("kubernetes.getPods") is True
        assert guard.is_allowed("cloudwatch.getMetricData") is True
        assert guard.is_allowed("slack.postMessage") is True

    def test_returns_false_for_write_operations(self, guard: ReadOnlyGuard):
        assert guard.is_allowed("kubernetes.delete") is False
        assert guard.is_allowed("aws.terminateInstances") is False
        assert guard.is_allowed("cloudwatch.putMetricAlarm") is False

    def test_returns_false_for_unknown_mcp(self, guard: ReadOnlyGuard):
        assert guard.is_allowed("unknown.anything") is False


class TestErrorMessages:
    """Tests that error messages are informative."""

    def test_violation_error_contains_operation(self, guard: ReadOnlyGuard):
        with pytest.raises(ReadOnlyViolationError) as exc_info:
            guard.validate_invocation("kubernetes.delete")
        assert "kubernetes.delete" in str(exc_info.value)

    def test_violation_error_contains_mcp(self, guard: ReadOnlyGuard):
        with pytest.raises(ReadOnlyViolationError) as exc_info:
            guard.validate_invocation("kubernetes.delete")
        assert "kubernetes" in str(exc_info.value)

    def test_violation_error_mentions_read_only(self, guard: ReadOnlyGuard):
        with pytest.raises(ReadOnlyViolationError) as exc_info:
            guard.validate_invocation("aws.terminateInstances")
        assert "read-only" in str(exc_info.value).lower()
