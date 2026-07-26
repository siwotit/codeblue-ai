"""Read-Only Enforcement Guard for CodeBlue AI.

This module provides the safety-critical enforcement layer that ensures CodeBlue AI
never modifies infrastructure. It validates every tool invocation before execution
and rejects any write/create/update/delete operations against monitored systems.

The guard is designed to fail closed: unknown operations that cannot be positively
identified as read-only are rejected rather than permitted.

Only Slack MCP write operations (postMessage, updateMessage) are permitted.

Requirements: 10.1, 10.2, 10.3, 10.5, 10.6
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants: Operation Classification
# ---------------------------------------------------------------------------

# Read-only verb prefixes that positively identify safe operations
READ_VERB_PREFIXES: tuple[str, ...] = (
    "get",
    "describe",
    "list",
    "query",
    "lookup",
    "filter",
    "search",
    "read",
    "fetch",
    "check",
)

# Write verb prefixes that indicate a mutating operation
WRITE_VERB_PREFIXES: tuple[str, ...] = (
    "create",
    "update",
    "delete",
    "put",
    "remove",
    "modify",
    "set",
    "attach",
    "detach",
    "revoke",
    "authorize",
    "terminate",
    "stop",
    "reboot",
    "scale",
    "apply",
    "patch",
    "rollout",
    "drain",
    "cordon",
    "taint",
    "exec",
    "run",
    "start",
    "kill",
    "restart",
    "destroy",
    "purge",
    "drop",
    "truncate",
    "insert",
    "write",
    "send",
    "post",
    "publish",
    "invoke",
)

# MCPs that are strictly read-only — no write operations permitted
READ_ONLY_MCPS: frozenset[str] = frozenset({
    "cloudwatch",
    "grafana",
    "kubernetes",
    "aws",
})

# The ONLY MCP permitted to perform write operations
WRITE_PERMITTED_MCPS: frozenset[str] = frozenset({"slack"})

# Specific Slack operations that are allowed (scoped to messaging only)
SLACK_ALLOWED_WRITES: frozenset[str] = frozenset({
    "slack.postMessage",
    "slack.updateMessage",
})

# Exhaustive allowlist of all read operations permitted across all MCPs
ALLOWED_OPERATIONS: frozenset[str] = frozenset({
    # CloudWatch MCP — read/describe/list/get only
    "cloudwatch.describeAlarms",
    "cloudwatch.getMetricData",
    "cloudwatch.getMetricStatistics",
    "cloudwatch.listMetrics",
    "cloudwatch.queryLogInsights",
    "cloudwatch.describeLogGroups",
    "cloudwatch.getLogEvents",
    "cloudwatch.filterLogEvents",
    "cloudwatch.listLogGroups",
    "cloudwatch.describeMetricFilters",
    "cloudwatch.getInsightRuleReport",
    # Grafana MCP — read-only dashboard and alert access
    "grafana.getAlertRules",
    "grafana.getDashboards",
    "grafana.getDashboardPanels",
    "grafana.queryMetrics",
    "grafana.getAnnotations",
    "grafana.listDatasources",
    "grafana.getFolder",
    "grafana.listFolders",
    # Kubernetes MCP — read-only cluster inspection
    "kubernetes.getPods",
    "kubernetes.getNodes",
    "kubernetes.getEvents",
    "kubernetes.getDeployments",
    "kubernetes.getReplicaSets",
    "kubernetes.getServices",
    "kubernetes.getNamespaces",
    "kubernetes.describeNode",
    "kubernetes.describePod",
    "kubernetes.getAddonStatus",
    "kubernetes.getHPA",
    "kubernetes.getLogs",
    "kubernetes.getConfigMaps",
    "kubernetes.getSecrets",
    "kubernetes.getIngresses",
    "kubernetes.getStatefulSets",
    "kubernetes.getDaemonSets",
    "kubernetes.getJobs",
    "kubernetes.getCronJobs",
    "kubernetes.getEndpoints",
    "kubernetes.getResourceQuotas",
    "kubernetes.getNetworkPolicies",
    # AWS API MCP — read-only AWS access
    "aws.lookupCloudTrailEvents",
    "aws.describeEksCluster",
    "aws.listEksClusters",
    "aws.describeInstances",
    "aws.describeAutoScalingGroups",
    "aws.describeLoadBalancers",
    "aws.describeTargetGroups",
    "aws.getCallerIdentity",
    "aws.listRoles",
    "aws.getRolePolicy",
    "aws.listTags",
    "aws.describeSubnets",
    "aws.describeSecurityGroups",
    "aws.describeVpcs",
    # Slack MCP — messaging only (the sole permitted write target)
    "slack.postMessage",
    "slack.updateMessage",
})

# Explicitly denied operations — known dangerous mutations
DENIED_OPERATIONS: frozenset[str] = frozenset({
    # CloudWatch write operations
    "cloudwatch.putMetricAlarm",
    "cloudwatch.deleteAlarms",
    "cloudwatch.setAlarmState",
    "cloudwatch.putMetricData",
    "cloudwatch.createLogGroup",
    "cloudwatch.deleteLogGroup",
    "cloudwatch.putLogEvents",
    "cloudwatch.putRetentionPolicy",
    "cloudwatch.deleteRetentionPolicy",
    # Grafana write operations
    "grafana.createAlertRule",
    "grafana.updateAlertRule",
    "grafana.deleteAlertRule",
    "grafana.createDashboard",
    "grafana.updateDashboard",
    "grafana.deleteDashboard",
    "grafana.createFolder",
    "grafana.deleteFolder",
    # Kubernetes write operations
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
    "kubernetes.createDeployment",
    "kubernetes.deleteDeployment",
    "kubernetes.createService",
    "kubernetes.deleteService",
    "kubernetes.createConfigMap",
    "kubernetes.deleteConfigMap",
    # AWS write operations
    "aws.createStack",
    "aws.updateStack",
    "aws.deleteStack",
    "aws.runInstances",
    "aws.terminateInstances",
    "aws.stopInstances",
    "aws.rebootInstances",
    "aws.createCluster",
    "aws.deleteCluster",
    "aws.updateClusterConfig",
    "aws.putRolePolicy",
    "aws.deleteRolePolicy",
    "aws.attachRolePolicy",
    "aws.detachRolePolicy",
    "aws.createSecurityGroup",
    "aws.deleteSecurityGroup",
    "aws.authorizeSecurityGroupIngress",
    "aws.revokeSecurityGroupIngress",
    "aws.createAutoScalingGroup",
    "aws.deleteAutoScalingGroup",
    "aws.updateAutoScalingGroup",
})


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RejectedInvocation:
    """Record of a rejected tool invocation for audit purposes.

    Requirement 10.6: Log the attempted operation without executing it.
    """

    operation: str
    mcp: str
    timestamp: str  # ISO 8601
    reason: str
    context: dict[str, Any] = field(default_factory=dict)


class ReadOnlyViolationError(Exception):
    """Raised when a write operation is attempted against a monitored system.

    This is a safety-critical error — the operation MUST NOT be executed.
    """

    def __init__(self, operation: str, mcp: str, reason: str) -> None:
        self.operation = operation
        self.mcp = mcp
        self.reason = reason
        super().__init__(
            f"READ-ONLY VIOLATION: Operation '{operation}' rejected. "
            f"MCP '{mcp}' is read-only. Reason: {reason}"
        )


# ---------------------------------------------------------------------------
# Read-Only Guard Implementation
# ---------------------------------------------------------------------------


class ReadOnlyGuard:
    """Enforces read-only constraints on all tool invocations.

    This is the primary enforcement mechanism that the workflow orchestrator
    uses to validate every MCP operation before execution. It is designed
    to fail closed — if an operation cannot be positively identified as
    read-only, it is rejected.

    Requirements:
        10.1: Expose only read/describe/list/get operations for monitored systems
        10.2: Do NOT expose create/update/delete/mutate operations
        10.3: Only Slack MCP may perform writes (postMessage, updateMessage)
        10.5: Orchestrator SHALL NOT invoke write tools
        10.6: Reject and log attempted write operations
    """

    def __init__(self) -> None:
        self._rejection_log: list[RejectedInvocation] = []

    def validate_invocation(
        self,
        operation: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> bool:
        """Validate that a tool invocation is permitted under read-only constraints.

        This method MUST be called before every MCP tool invocation. It validates
        the operation against the allowlist and rejects write operations.

        Args:
            operation: Fully qualified operation name (e.g., "kubernetes.getPods").
            context: Optional additional context for audit logging.

        Returns:
            True if the operation is permitted.

        Raises:
            ReadOnlyViolationError: If the operation would modify a monitored system.
        """
        mcp, action = self._parse_operation(operation)

        # Step 1: Check explicit allowlist — fast path for known-safe operations
        if operation in ALLOWED_OPERATIONS:
            return True

        # Step 2: Check explicit denylist — fast rejection for known-dangerous operations
        if operation in DENIED_OPERATIONS:
            self._reject(operation, mcp, "operation in explicit denylist", context)
            raise ReadOnlyViolationError(operation, mcp, "operation in explicit denylist")

        # Step 3: Check if Slack MCP — only allowed for specific write operations
        if mcp in WRITE_PERMITTED_MCPS:
            if operation in SLACK_ALLOWED_WRITES:
                return True
            # For Slack, allow read-like operations (get/list/search)
            if self._is_read_verb(action):
                return True
            # Any other operation on Slack is rejected — only postMessage
            # and updateMessage are permitted writes
            reason = "Slack writes limited to postMessage and updateMessage"
            self._reject(operation, mcp, reason, context)
            raise ReadOnlyViolationError(operation, mcp, reason)

        # Step 4: For read-only MCPs, reject any write verb
        if mcp in READ_ONLY_MCPS:
            if self._is_write_verb(action):
                reason = f"write verb detected on read-only MCP '{mcp}'"
                self._reject(operation, mcp, reason, context)
                raise ReadOnlyViolationError(operation, mcp, reason)
            # Unknown read-like operation on a known MCP — allow
            if self._is_read_verb(action):
                return True
            # Ambiguous verb on a read-only MCP — fail closed
            reason = (
                f"operation '{action}' on read-only MCP '{mcp}' cannot be "
                f"positively identified as read-only (fail closed)"
            )
            self._reject(operation, mcp, reason, context)
            raise ReadOnlyViolationError(operation, mcp, reason)

        # Step 5: Unknown MCP — fail closed
        reason = f"unknown MCP '{mcp}' not in permitted list (fail closed)"
        self._reject(operation, mcp, reason, context)
        raise ReadOnlyViolationError(operation, mcp, reason)

    def is_allowed(self, operation: str) -> bool:
        """Check if an operation is allowed without raising an exception.

        Args:
            operation: Fully qualified operation name.

        Returns:
            True if allowed, False if it would be rejected.
        """
        try:
            return self.validate_invocation(operation)
        except ReadOnlyViolationError:
            return False

    def get_rejection_log(self) -> list[RejectedInvocation]:
        """Return the audit log of all rejected invocations.

        Returns:
            List of RejectedInvocation records in chronological order.
        """
        return list(self._rejection_log)

    def clear_rejection_log(self) -> None:
        """Clear the rejection audit log. For testing only."""
        self._rejection_log.clear()

    @property
    def rejection_count(self) -> int:
        """Number of rejected operations since last clear."""
        return len(self._rejection_log)

    # -----------------------------------------------------------------------
    # Private Helpers
    # -----------------------------------------------------------------------

    def _parse_operation(self, operation: str) -> tuple[str, str]:
        """Parse a fully qualified operation into (mcp, action).

        Args:
            operation: e.g., "kubernetes.getPods"

        Returns:
            Tuple of (mcp_name, action_name).

        Raises:
            ReadOnlyViolationError: If the operation name is malformed.
        """
        if "." not in operation:
            self._reject(
                operation, "unknown",
                "malformed operation name (missing MCP prefix)",
                None,
            )
            raise ReadOnlyViolationError(
                operation, "unknown",
                "malformed operation name (missing MCP prefix)",
            )
        parts = operation.split(".", 1)
        return parts[0], parts[1]

    def _is_write_verb(self, action: str) -> bool:
        """Check if an action name starts with a write verb prefix."""
        action_lower = action.lower()
        return any(action_lower.startswith(prefix) for prefix in WRITE_VERB_PREFIXES)

    def _is_read_verb(self, action: str) -> bool:
        """Check if an action name starts with a read verb prefix."""
        action_lower = action.lower()
        return any(action_lower.startswith(prefix) for prefix in READ_VERB_PREFIXES)

    def _reject(
        self,
        operation: str,
        mcp: str,
        reason: str,
        context: dict[str, Any] | None,
    ) -> None:
        """Record a rejected invocation in the audit log.

        Requirement 10.6: Log the attempted operation without executing it.
        """
        record = RejectedInvocation(
            operation=operation,
            mcp=mcp,
            timestamp=datetime.now(timezone.utc).isoformat(),
            reason=reason,
            context=context or {},
        )
        self._rejection_log.append(record)
        logger.warning(
            "READ-ONLY GUARD REJECTED: operation=%s mcp=%s reason=%s",
            operation,
            mcp,
            reason,
        )
