"""CodeBlue AI Tool Registry.

Defines all skills as tools with input/output schemas, enforces read-only
access patterns via allowlists and denylists, and validates that write
operations are never invoked against monitored systems.

Requirements: 10.5, 10.6
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class ToolCategory(str, Enum):
    """Category of a registered tool/skill."""

    SIGNAL_COLLECTION = "signal_collection"
    KUBERNETES_HEALTH = "kubernetes_health"
    REASONING = "reasoning"
    FORMATTING = "formatting"


@dataclass(frozen=True)
class ToolSchema:
    """Input or output schema definition for a tool."""

    type: str  # "object", "string", etc.
    properties: dict[str, Any] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    description: str = ""


@dataclass(frozen=True)
class ToolDefinition:
    """Definition of a registered skill/tool."""

    name: str
    description: str
    category: ToolCategory
    input_schema: ToolSchema
    output_schema: ToolSchema
    timeout_seconds: int = 15


@dataclass(frozen=True)
class RejectedOperation:
    """Record of a rejected write operation for audit purposes.

    Requirement 10.6: Log the attempted operation without executing it.
    """

    operation: str
    target: str
    timestamp: str  # ISO 8601
    reason: str


class WriteOperationError(Exception):
    """Raised when a write operation is attempted against a monitored system."""

    def __init__(self, operation: str, target: str) -> None:
        self.operation = operation
        self.target = target
        super().__init__(
            f"Write operation rejected: '{operation}' against '{target}'. "
            f"CodeBlue AI is strictly read-only."
        )


# ---------------------------------------------------------------------------
# Read-only enforcement: Allowlist and Denylist
# ---------------------------------------------------------------------------

# Operations that are permitted across all MCPs (read-only patterns)
MCP_OPERATION_ALLOWLIST: set[str] = frozenset(
    {
        # CloudWatch MCP
        "cloudwatch.describeAlarms",
        "cloudwatch.getMetricData",
        "cloudwatch.getMetricStatistics",
        "cloudwatch.listMetrics",
        "cloudwatch.queryLogInsights",
        "cloudwatch.describeLogGroups",
        "cloudwatch.getLogEvents",
        "cloudwatch.filterLogEvents",
        # Grafana MCP
        "grafana.getAlertRules",
        "grafana.getDashboards",
        "grafana.getDashboardPanels",
        "grafana.queryMetrics",
        "grafana.getAnnotations",
        # Kubernetes MCP
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
        # AWS API MCP
        "aws.lookupCloudTrailEvents",
        "aws.describeEksCluster",
        "aws.listEksClusters",
        "aws.describeInstances",
        "aws.describeAutoScalingGroups",
        # Slack MCP (only write-permitted MCP)
        "slack.postMessage",
        "slack.updateMessage",
    }
)


# Operations that MUST NEVER be invoked against monitored systems.
# Slack write ops are intentionally excluded from this denylist since
# posting messages is the sole permitted write action.
MCP_OPERATION_DENYLIST: set[str] = frozenset(
    {
        # CloudWatch write operations
        "cloudwatch.putMetricAlarm",
        "cloudwatch.deleteAlarms",
        "cloudwatch.setAlarmState",
        "cloudwatch.putMetricData",
        "cloudwatch.createLogGroup",
        "cloudwatch.deleteLogGroup",
        "cloudwatch.putLogEvents",
        # Grafana write operations
        "grafana.createAlertRule",
        "grafana.updateAlertRule",
        "grafana.deleteAlertRule",
        "grafana.createDashboard",
        "grafana.updateDashboard",
        "grafana.deleteDashboard",
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
    }
)

# Verb prefixes that indicate a write operation
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
)


# Slack is the ONLY MCP permitted to perform write operations
WRITE_PERMITTED_MCPS: frozenset[str] = frozenset({"slack"})


def validate_operation(operation: str) -> bool:
    """Validate that an operation is permitted under read-only constraints.

    Requirement 10.5: The Orchestrator SHALL NOT invoke any tool that creates,
    modifies, or deletes infrastructure resources.

    Requirement 10.6: IF the Orchestrator receives a tool invocation request
    targeting a write/create/update/delete operation on a monitored system,
    THEN it SHALL reject the invocation and log the attempted operation
    without executing it.

    Args:
        operation: Fully qualified operation name (e.g., "kubernetes.getPods").

    Returns:
        True if the operation is allowed.

    Raises:
        WriteOperationError: If the operation is a write/mutate against a
            monitored system.
    """
    # Explicitly allowed operations always pass
    if operation in MCP_OPERATION_ALLOWLIST:
        return True

    # Explicitly denied operations always fail
    if operation in MCP_OPERATION_DENYLIST:
        mcp_name = operation.split(".")[0] if "." in operation else "unknown"
        _log_rejected_operation(operation, mcp_name, "operation in explicit denylist")
        raise WriteOperationError(operation=operation, target=mcp_name)

    # For unknown operations, check if the verb indicates a write
    mcp_name, _, action = operation.partition(".")
    if not action:
        _log_rejected_operation(operation, "unknown", "malformed operation name")
        raise WriteOperationError(operation=operation, target="unknown")

    # Slack writes are permitted
    if mcp_name in WRITE_PERMITTED_MCPS:
        return True

    # Check verb prefixes for write indicators
    action_lower = action.lower()
    for prefix in WRITE_VERB_PREFIXES:
        if action_lower.startswith(prefix):
            _log_rejected_operation(
                operation, mcp_name, f"write verb prefix '{prefix}' detected"
            )
            raise WriteOperationError(operation=operation, target=mcp_name)

    # Unknown read-like operations are permitted with a warning logged
    logger.debug("Permitting unknown operation '%s' (no write verb detected)", operation)
    return True


# ---------------------------------------------------------------------------
# Rejection Audit Log
# ---------------------------------------------------------------------------

_rejected_operations: list[RejectedOperation] = []


def _log_rejected_operation(operation: str, target: str, reason: str) -> None:
    """Log a rejected write operation for audit purposes.

    Requirement 10.6: log the attempted operation without executing it.
    """
    record = RejectedOperation(
        operation=operation,
        target=target,
        timestamp=datetime.now(timezone.utc).isoformat(),
        reason=reason,
    )
    _rejected_operations.append(record)
    logger.warning(
        "WRITE OPERATION REJECTED: operation=%s target=%s reason=%s",
        operation,
        target,
        reason,
    )


def get_rejected_operations() -> list[RejectedOperation]:
    """Return the audit log of rejected write operations.

    Returns:
        List of RejectedOperation records, ordered chronologically.
    """
    return list(_rejected_operations)


def clear_rejected_operations() -> None:
    """Clear the rejection audit log (for testing purposes only)."""
    _rejected_operations.clear()


# ---------------------------------------------------------------------------
# Skill/Tool Definitions
# ---------------------------------------------------------------------------

SKILL_REGISTRY: dict[str, ToolDefinition] = {}


def _register(tool: ToolDefinition) -> ToolDefinition:
    """Register a tool definition in the global registry."""
    SKILL_REGISTRY[tool.name] = tool
    return tool


# --- Signal Collection Skills (Script-Heavy) ---

_register(
    ToolDefinition(
        name="alert-ingestion",
        description=(
            "Normalizes alerts from heterogeneous sources (CloudWatch, Grafana, "
            "K8s events) into a common NormalizedAlert format."
        ),
        category=ToolCategory.SIGNAL_COLLECTION,
        input_schema=ToolSchema(
            type="object",
            description="Raw alarm payload from a monitoring source.",
            properties={
                "source": {
                    "type": "string",
                    "enum": ["cloudwatch", "grafana", "kubernetes"],
                    "description": "Origin monitoring system.",
                },
                "payload": {
                    "type": "object",
                    "description": "Raw alarm/event JSON from the source system.",
                },
            },
            required=["source", "payload"],
        ),
        output_schema=ToolSchema(
            type="object",
            description="NormalizedAlert with all required fields populated.",
            properties={
                "status": {"type": "string", "enum": ["success", "error"]},
                "alert": {"type": "object", "description": "NormalizedAlert JSON."},
                "error": {"type": "string", "description": "Error message if failed."},
            },
            required=["status"],
        ),
    )
)


_register(
    ToolDefinition(
        name="metric-baseline",
        description=(
            "Compares current metric values against historical baselines (7-day "
            "and optional 30-day) to classify deviation severity."
        ),
        category=ToolCategory.SIGNAL_COLLECTION,
        input_schema=ToolSchema(
            type="object",
            description="Metric query parameters for baseline comparison.",
            properties={
                "metricName": {"type": "string", "description": "CloudWatch metric name."},
                "namespace": {"type": "string", "description": "Metric namespace."},
                "dimensions": {
                    "type": "object",
                    "description": "Key-value metric dimensions.",
                },
                "currentWindow": {
                    "type": "object",
                    "description": "TimeRange for the current observation period.",
                    "properties": {
                        "start": {"type": "string", "format": "date-time"},
                        "end": {"type": "string", "format": "date-time"},
                    },
                },
                "baselineWindow": {
                    "type": "string",
                    "enum": ["7d", "30d"],
                    "description": "Baseline comparison period.",
                },
            },
            required=["metricName", "namespace", "dimensions", "currentWindow"],
        ),
        output_schema=ToolSchema(
            type="object",
            description="MetricDeviation with classification and confidence.",
            properties={
                "status": {"type": "string", "enum": ["success", "error"]},
                "deviation": {"type": "object", "description": "MetricDeviation JSON."},
                "error": {"type": "string"},
            },
            required=["status"],
        ),
    )
)


_register(
    ToolDefinition(
        name="deploy-correlation",
        description=(
            "Searches for recent changes (deployments, config changes, IAM changes) "
            "that overlap temporally and spatially with the incident."
        ),
        category=ToolCategory.SIGNAL_COLLECTION,
        input_schema=ToolSchema(
            type="object",
            description="Correlation request with affected resources and time context.",
            properties={
                "affectedResources": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of ResourceIdentifier objects.",
                },
                "incidentTime": {
                    "type": "string",
                    "format": "date-time",
                    "description": "ISO 8601 timestamp of incident start.",
                },
                "lookbackWindow": {
                    "type": "string",
                    "default": "24h",
                    "description": "How far back to search for changes.",
                },
            },
            required=["affectedResources", "incidentTime"],
        ),
        output_schema=ToolSchema(
            type="object",
            description="CorrelatedChanges with scored change list.",
            properties={
                "status": {"type": "string", "enum": ["success", "error"]},
                "correlatedChanges": {"type": "object", "description": "CorrelatedChanges JSON."},
                "error": {"type": "string"},
            },
            required=["status"],
        ),
    )
)

_register(
    ToolDefinition(
        name="log-triage",
        description=(
            "Queries application and infrastructure logs to find error patterns, "
            "exception spikes, and timeout increases coinciding with the incident."
        ),
        category=ToolCategory.SIGNAL_COLLECTION,
        input_schema=ToolSchema(
            type="object",
            description="Log triage request with log groups and time range.",
            properties={
                "logGroups": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CloudWatch log group names to query.",
                },
                "namespaces": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "K8s namespaces for pod log queries (optional).",
                },
                "timeRange": {
                    "type": "object",
                    "description": "TimeRange for log queries.",
                    "properties": {
                        "start": {"type": "string", "format": "date-time"},
                        "end": {"type": "string", "format": "date-time"},
                    },
                },
                "errorPatterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional custom error patterns to search for.",
                },
            },
            required=["logGroups", "timeRange"],
        ),
        output_schema=ToolSchema(
            type="object",
            description="LogFindings with classified error patterns.",
            properties={
                "status": {"type": "string", "enum": ["success", "error"]},
                "logFindings": {"type": "object", "description": "LogFindings JSON."},
                "error": {"type": "string"},
            },
            required=["status"],
        ),
    )
)


# --- Kubernetes Health Skills (Mixed) ---

_register(
    ToolDefinition(
        name="k8s-cluster-health",
        description=(
            "Checks overall Kubernetes cluster state: node readiness, pending pods, "
            "resource pressure, and recent warning events."
        ),
        category=ToolCategory.KUBERNETES_HEALTH,
        input_schema=ToolSchema(
            type="object",
            description="Cluster identification for health check.",
            properties={
                "clusterName": {
                    "type": "string",
                    "description": "Name of the EKS/K8s cluster to check.",
                },
                "region": {
                    "type": "string",
                    "description": "AWS region of the cluster (optional).",
                },
            },
            required=["clusterName"],
        ),
        output_schema=ToolSchema(
            type="object",
            description="ClusterHealthReport with node, pod, addon, and event summaries.",
            properties={
                "status": {"type": "string", "enum": ["success", "error"]},
                "clusterHealth": {"type": "object", "description": "ClusterHealthReport JSON."},
                "error": {"type": "string"},
            },
            required=["status"],
        ),
    )
)

_register(
    ToolDefinition(
        name="pod-failure-triage",
        description=(
            "Classifies pod failures by reason (CrashLoopBackOff, OOMKilled, "
            "ImagePullBackOff, CreateContainerError, SandboxError) with namespace context."
        ),
        category=ToolCategory.KUBERNETES_HEALTH,
        input_schema=ToolSchema(
            type="object",
            description="Pod failure query parameters.",
            properties={
                "namespace": {
                    "type": "string",
                    "description": "K8s namespace to inspect.",
                },
                "labelSelector": {
                    "type": "string",
                    "description": "Optional label selector to filter pods.",
                },
                "clusterName": {
                    "type": "string",
                    "description": "Cluster context for the query.",
                },
            },
            required=["namespace"],
        ),
        output_schema=ToolSchema(
            type="object",
            description="PodFailureReport with classified failures.",
            properties={
                "status": {"type": "string", "enum": ["success", "error"]},
                "podFailures": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of PodFailure objects.",
                },
                "error": {"type": "string"},
            },
            required=["status"],
        ),
    )
)


_register(
    ToolDefinition(
        name="node-condition-check",
        description=(
            "Detects NotReady nodes, MemoryPressure, DiskPressure, PIDPressure, "
            "and NetworkUnavailable conditions across cluster nodes."
        ),
        category=ToolCategory.KUBERNETES_HEALTH,
        input_schema=ToolSchema(
            type="object",
            description="Cluster identification for node condition check.",
            properties={
                "clusterName": {
                    "type": "string",
                    "description": "Name of the K8s cluster.",
                },
            },
            required=["clusterName"],
        ),
        output_schema=ToolSchema(
            type="object",
            description="NodeConditionReport with detected conditions.",
            properties={
                "status": {"type": "string", "enum": ["success", "error"]},
                "nodeConditions": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of NodeCondition objects.",
                },
                "error": {"type": "string"},
            },
            required=["status"],
        ),
    )
)

_register(
    ToolDefinition(
        name="eks-addon-status",
        description=(
            "Checks health of EKS addons: VPC CNI, CoreDNS, kube-proxy, and EBS CSI. "
            "Triggers degraded finding when any addon is degraded or failed."
        ),
        category=ToolCategory.KUBERNETES_HEALTH,
        input_schema=ToolSchema(
            type="object",
            description="EKS cluster identification for addon status check.",
            properties={
                "clusterName": {
                    "type": "string",
                    "description": "Name of the EKS cluster.",
                },
            },
            required=["clusterName"],
        ),
        output_schema=ToolSchema(
            type="object",
            description="AddonHealthReport with status for each managed addon.",
            properties={
                "status": {"type": "string", "enum": ["success", "error"]},
                "addons": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of AddonHealth objects.",
                },
                "error": {"type": "string"},
            },
            required=["status"],
        ),
    )
)


# --- Reasoning Skills (Prompt-Heavy) ---

_register(
    ToolDefinition(
        name="hypothesis-engine",
        description=(
            "Given all collected signals, produces a ranked list of probable root "
            "causes with confidence levels and supporting evidence chains."
        ),
        category=ToolCategory.REASONING,
        input_schema=ToolSchema(
            type="object",
            description="IncidentContext with all collected signals.",
            properties={
                "alert": {"type": "object", "description": "NormalizedAlert."},
                "metricDeviation": {"type": "object", "description": "MetricDeviation result."},
                "correlatedChanges": {"type": "object", "description": "CorrelatedChanges result."},
                "logFindings": {"type": "object", "description": "LogFindings result."},
                "clusterHealth": {
                    "type": "object",
                    "description": "ClusterHealthReport (optional, K8s only).",
                },
            },
            required=["alert"],
        ),
        output_schema=ToolSchema(
            type="object",
            description="RankedHypotheses with 1-10 hypotheses in descending confidence.",
            properties={
                "status": {"type": "string", "enum": ["success", "error"]},
                "hypotheses": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of Hypothesis objects, ranked by confidence.",
                },
                "error": {"type": "string"},
            },
            required=["status"],
        ),
    )
)

_register(
    ToolDefinition(
        name="escalation-decision",
        description=(
            "Determines whether an incident requires immediate human intervention "
            "(page on-call) or can wait, based on severity, blast radius, and "
            "service criticality."
        ),
        category=ToolCategory.REASONING,
        input_schema=ToolSchema(
            type="object",
            description="IncidentReport for escalation evaluation.",
            properties={
                "severity": {
                    "type": "string",
                    "enum": ["critical", "high", "medium", "low"],
                    "description": "Assessed incident severity.",
                },
                "blastRadius": {"type": "object", "description": "BlastRadius assessment."},
                "hypotheses": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Ranked hypotheses.",
                },
                "timeOfDay": {
                    "type": "string",
                    "format": "time",
                    "description": "Current local time for business-hours check.",
                },
                "serviceCriticality": {
                    "type": "string",
                    "description": "Service tier (e.g., 'tier-1') if available.",
                },
            },
            required=["severity", "blastRadius"],
        ),
        output_schema=ToolSchema(
            type="object",
            description="EscalationDecision with urgency and rationale.",
            properties={
                "status": {"type": "string", "enum": ["success", "error"]},
                "decision": {"type": "object", "description": "EscalationDecision JSON."},
                "error": {"type": "string"},
            },
            required=["status"],
        ),
    )
)


# --- Formatting Skills (Mixed) ---

_register(
    ToolDefinition(
        name="incident-summary-format",
        description=(
            "Formats the IncidentReport into a Slack Block Kit message with "
            "severity indicators, sections, and plain-text fallback."
        ),
        category=ToolCategory.FORMATTING,
        input_schema=ToolSchema(
            type="object",
            description="Complete IncidentReport for formatting.",
            properties={
                "incidentReport": {"type": "object", "description": "Full IncidentReport JSON."},
            },
            required=["incidentReport"],
        ),
        output_schema=ToolSchema(
            type="object",
            description="SlackMessage with Block Kit blocks and fallback text.",
            properties={
                "status": {"type": "string", "enum": ["success", "error"]},
                "message": {"type": "object", "description": "SlackMessage JSON."},
                "error": {"type": "string"},
            },
            required=["status"],
        ),
    )
)

_register(
    ToolDefinition(
        name="evidence-provenance",
        description=(
            "Attaches source references (console URLs, kubectl commands, timestamps) "
            "to every claim in the incident report for independent verification."
        ),
        category=ToolCategory.FORMATTING,
        input_schema=ToolSchema(
            type="object",
            description="Findings list to annotate with provenance.",
            properties={
                "findings": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of Finding objects with claims to annotate.",
                },
                "region": {"type": "string", "description": "AWS region for URL generation."},
                "clusterContext": {
                    "type": "string",
                    "description": "K8s cluster context for kubectl commands (optional).",
                },
            },
            required=["findings"],
        ),
        output_schema=ToolSchema(
            type="object",
            description="ProvenanceAnnotatedFindings with source links.",
            properties={
                "status": {"type": "string", "enum": ["success", "error"]},
                "annotatedFindings": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of ProvenanceAnnotatedFinding objects.",
                },
                "error": {"type": "string"},
            },
            required=["status"],
        ),
    )
)


# ---------------------------------------------------------------------------
# Registry Access Functions
# ---------------------------------------------------------------------------


def get_tool(name: str) -> ToolDefinition | None:
    """Look up a tool by name.

    Args:
        name: The skill/tool name (e.g., "metric-baseline").

    Returns:
        The ToolDefinition if found, None otherwise.
    """
    return SKILL_REGISTRY.get(name)


def get_tools_by_category(category: ToolCategory) -> list[ToolDefinition]:
    """Get all tools in a given category.

    Args:
        category: The ToolCategory to filter by.

    Returns:
        List of ToolDefinitions matching the category.
    """
    return [t for t in SKILL_REGISTRY.values() if t.category == category]


def list_tool_names() -> list[str]:
    """List all registered tool names.

    Returns:
        Sorted list of tool name strings.
    """
    return sorted(SKILL_REGISTRY.keys())


def is_operation_allowed(operation: str) -> bool:
    """Check if an operation is allowed without raising an exception.

    Args:
        operation: Fully qualified operation name.

    Returns:
        True if allowed, False if it would be rejected.
    """
    try:
        return validate_operation(operation)
    except WriteOperationError:
        return False
