"""Tests for the CodeBlue AI tool registry.

Validates tool registration, read-only enforcement via allowlists/denylists,
and the write operation validation logic.
"""

import pytest

from orchestrator.tool_registry import (
    MCP_OPERATION_ALLOWLIST,
    MCP_OPERATION_DENYLIST,
    SKILL_REGISTRY,
    WRITE_PERMITTED_MCPS,
    RejectedOperation,
    ToolCategory,
    WriteOperationError,
    clear_rejected_operations,
    get_rejected_operations,
    get_tool,
    get_tools_by_category,
    is_operation_allowed,
    list_tool_names,
    validate_operation,
)


class TestToolRegistration:
    """Tests for skill/tool registration."""

    def test_all_twelve_skills_registered(self):
        assert len(SKILL_REGISTRY) == 12

    def test_expected_skill_names(self):
        expected = {
            "alert-ingestion",
            "metric-baseline",
            "deploy-correlation",
            "log-triage",
            "k8s-cluster-health",
            "pod-failure-triage",
            "node-condition-check",
            "eks-addon-status",
            "hypothesis-engine",
            "escalation-decision",
            "incident-summary-format",
            "evidence-provenance",
        }
        assert set(SKILL_REGISTRY.keys()) == expected

    def test_signal_collection_skills(self):
        tools = get_tools_by_category(ToolCategory.SIGNAL_COLLECTION)
        names = {t.name for t in tools}
        assert names == {
            "alert-ingestion",
            "metric-baseline",
            "deploy-correlation",
            "log-triage",
        }

    def test_kubernetes_health_skills(self):
        tools = get_tools_by_category(ToolCategory.KUBERNETES_HEALTH)
        names = {t.name for t in tools}
        assert names == {
            "k8s-cluster-health",
            "pod-failure-triage",
            "node-condition-check",
            "eks-addon-status",
        }

    def test_reasoning_skills(self):
        tools = get_tools_by_category(ToolCategory.REASONING)
        names = {t.name for t in tools}
        assert names == {"hypothesis-engine", "escalation-decision"}

    def test_formatting_skills(self):
        tools = get_tools_by_category(ToolCategory.FORMATTING)
        names = {t.name for t in tools}
        assert names == {"incident-summary-format", "evidence-provenance"}

    def test_get_tool_returns_definition(self):
        tool = get_tool("metric-baseline")
        assert tool is not None
        assert tool.name == "metric-baseline"
        assert tool.category == ToolCategory.SIGNAL_COLLECTION
        assert tool.timeout_seconds == 15

    def test_get_tool_returns_none_for_unknown(self):
        assert get_tool("nonexistent-skill") is None

    def test_list_tool_names_sorted(self):
        names = list_tool_names()
        assert names == sorted(names)
        assert len(names) == 12

    def test_all_tools_have_input_schema(self):
        for tool in SKILL_REGISTRY.values():
            assert tool.input_schema is not None
            assert tool.input_schema.type == "object"
            assert len(tool.input_schema.required) > 0

    def test_all_tools_have_output_schema(self):
        for tool in SKILL_REGISTRY.values():
            assert tool.output_schema is not None
            assert tool.output_schema.type == "object"
            assert "status" in tool.output_schema.required


class TestReadOnlyEnforcement:
    """Tests for the allowlist/denylist and write operation validation."""

    def test_allowlist_contains_read_operations(self):
        # Spot check key read operations
        assert "cloudwatch.describeAlarms" in MCP_OPERATION_ALLOWLIST
        assert "cloudwatch.getMetricData" in MCP_OPERATION_ALLOWLIST
        assert "kubernetes.getPods" in MCP_OPERATION_ALLOWLIST
        assert "kubernetes.getEvents" in MCP_OPERATION_ALLOWLIST
        assert "aws.lookupCloudTrailEvents" in MCP_OPERATION_ALLOWLIST

    def test_allowlist_includes_slack_writes(self):
        # Slack is the ONLY write-permitted MCP
        assert "slack.postMessage" in MCP_OPERATION_ALLOWLIST
        assert "slack.updateMessage" in MCP_OPERATION_ALLOWLIST

    def test_denylist_contains_write_operations(self):
        assert "kubernetes.delete" in MCP_OPERATION_DENYLIST
        assert "kubernetes.apply" in MCP_OPERATION_DENYLIST
        assert "kubernetes.scale" in MCP_OPERATION_DENYLIST
        assert "aws.terminateInstances" in MCP_OPERATION_DENYLIST
        assert "cloudwatch.putMetricAlarm" in MCP_OPERATION_DENYLIST

    def test_allowlist_and_denylist_do_not_overlap(self):
        overlap = MCP_OPERATION_ALLOWLIST & MCP_OPERATION_DENYLIST
        assert len(overlap) == 0, f"Overlap found: {overlap}"

    def test_only_slack_is_write_permitted(self):
        assert WRITE_PERMITTED_MCPS == frozenset({"slack"})

    def test_validate_allows_read_operations(self):
        assert validate_operation("cloudwatch.getMetricData") is True
        assert validate_operation("kubernetes.getPods") is True
        assert validate_operation("grafana.getAlertRules") is True

    def test_validate_allows_slack_writes(self):
        assert validate_operation("slack.postMessage") is True
        assert validate_operation("slack.updateMessage") is True

    def test_validate_rejects_explicit_denylist_operations(self):
        with pytest.raises(WriteOperationError) as exc_info:
            validate_operation("kubernetes.delete")
        assert exc_info.value.operation == "kubernetes.delete"
        assert exc_info.value.target == "kubernetes"

    def test_validate_rejects_unknown_write_verb_prefixes(self):
        with pytest.raises(WriteOperationError):
            validate_operation("aws.createNewResource")
        with pytest.raises(WriteOperationError):
            validate_operation("kubernetes.updateDeployment")
        with pytest.raises(WriteOperationError):
            validate_operation("cloudwatch.deleteAlarm")

    def test_validate_rejects_mutate_operations(self):
        with pytest.raises(WriteOperationError):
            validate_operation("aws.terminateInstances")
        with pytest.raises(WriteOperationError):
            validate_operation("kubernetes.rolloutRestart")
        with pytest.raises(WriteOperationError):
            validate_operation("kubernetes.drainNode")

    def test_validate_allows_unknown_read_like_operations(self):
        # Unknown operations without write verb prefixes are allowed
        assert validate_operation("cloudwatch.listSomething") is True
        assert validate_operation("kubernetes.getSomething") is True

    def test_is_operation_allowed_returns_bool(self):
        assert is_operation_allowed("kubernetes.getPods") is True
        assert is_operation_allowed("kubernetes.delete") is False
        assert is_operation_allowed("aws.terminateInstances") is False
        assert is_operation_allowed("slack.postMessage") is True

    def test_write_operation_error_message(self):
        with pytest.raises(WriteOperationError) as exc_info:
            validate_operation("kubernetes.apply")
        error = exc_info.value
        assert "read-only" in str(error).lower()
        assert error.operation == "kubernetes.apply"
        assert error.target == "kubernetes"


class TestRejectionAuditLog:
    """Tests for the write operation rejection audit log.

    Requirement 10.6: The Orchestrator SHALL reject the invocation and log
    the attempted operation without executing it.
    """

    def setup_method(self):
        """Clear the audit log before each test."""
        clear_rejected_operations()

    def test_rejected_operation_is_logged(self):
        with pytest.raises(WriteOperationError):
            validate_operation("kubernetes.delete")
        log = get_rejected_operations()
        assert len(log) == 1
        assert log[0].operation == "kubernetes.delete"
        assert log[0].target == "kubernetes"
        assert "denylist" in log[0].reason

    def test_multiple_rejections_accumulate(self):
        with pytest.raises(WriteOperationError):
            validate_operation("kubernetes.delete")
        with pytest.raises(WriteOperationError):
            validate_operation("aws.terminateInstances")
        with pytest.raises(WriteOperationError):
            validate_operation("cloudwatch.putMetricAlarm")
        log = get_rejected_operations()
        assert len(log) == 3
        operations = [r.operation for r in log]
        assert "kubernetes.delete" in operations
        assert "aws.terminateInstances" in operations
        assert "cloudwatch.putMetricAlarm" in operations

    def test_rejected_operation_has_timestamp(self):
        with pytest.raises(WriteOperationError):
            validate_operation("kubernetes.apply")
        log = get_rejected_operations()
        assert len(log) == 1
        # ISO 8601 format check (contains T separator and timezone info)
        assert "T" in log[0].timestamp
        assert "+" in log[0].timestamp or "Z" in log[0].timestamp

    def test_write_verb_prefix_rejection_is_logged(self):
        with pytest.raises(WriteOperationError):
            validate_operation("aws.createNewCluster")
        log = get_rejected_operations()
        assert len(log) == 1
        assert log[0].operation == "aws.createNewCluster"
        assert log[0].target == "aws"
        assert "write verb prefix" in log[0].reason

    def test_allowed_operations_are_not_logged(self):
        validate_operation("kubernetes.getPods")
        validate_operation("cloudwatch.getMetricData")
        validate_operation("slack.postMessage")
        log = get_rejected_operations()
        assert len(log) == 0

    def test_clear_rejected_operations(self):
        with pytest.raises(WriteOperationError):
            validate_operation("kubernetes.delete")
        assert len(get_rejected_operations()) == 1
        clear_rejected_operations()
        assert len(get_rejected_operations()) == 0

    def test_rejected_operation_dataclass_is_frozen(self):
        with pytest.raises(WriteOperationError):
            validate_operation("kubernetes.delete")
        record = get_rejected_operations()[0]
        assert isinstance(record, RejectedOperation)
        with pytest.raises(Exception):  # FrozenInstanceError
            record.operation = "modified"  # type: ignore[misc]
