"""Shared data models for CodeBlue AI skills.

All models follow the TypeScript interfaces defined in the design document,
translated to Python using Pydantic v2 for validation and serialization.
Models support camelCase JSON serialization via field aliases and roundtrip
through JSON (serialize then deserialize produces identical result).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# --- Enums ---


class AlertSource(str, Enum):
    """Source system for an alert."""

    CLOUDWATCH = "cloudwatch"
    GRAFANA = "grafana"
    KUBERNETES = "kubernetes"


class AlertState(str, Enum):
    """Current state of an alert."""

    FIRING = "firing"
    RESOLVED = "resolved"


class Severity(str, Enum):
    """Severity classification."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DeviationClassification(str, Enum):
    """Metric deviation classification."""

    NORMAL = "normal"
    ELEVATED = "elevated"
    ANOMALOUS = "anomalous"
    CRITICAL = "critical"


class ChangeType(str, Enum):
    """Type of infrastructure change."""

    DEPLOYMENT = "deployment"
    CONFIG_CHANGE = "config_change"
    IAM_CHANGE = "iam_change"
    SCALING_EVENT = "scaling_event"
    ROLLOUT = "rollout"
    ADDON_UPDATE = "addon_update"


class ChangeSource(str, Enum):
    """Source of a discovered change."""

    CLOUDTRAIL = "cloudtrail"
    KUBERNETES = "kubernetes"
    GRAFANA = "grafana"


class LogSeverity(str, Enum):
    """Severity of a log finding."""

    ERROR = "error"
    WARNING = "warning"
    FATAL = "fatal"


class ClusterHealth(str, Enum):
    """Overall cluster health status."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"


class PodFailureReason(str, Enum):
    """Reason for pod failure."""

    CRASH_LOOP_BACK_OFF = "CrashLoopBackOff"
    OOM_KILLED = "OOMKilled"
    IMAGE_PULL_BACK_OFF = "ImagePullBackOff"
    CREATE_CONTAINER_ERROR = "CreateContainerError"
    SANDBOX_ERROR = "SandboxError"


class NodeConditionType(str, Enum):
    """Type of node condition."""

    MEMORY_PRESSURE = "MemoryPressure"
    DISK_PRESSURE = "DiskPressure"
    PID_PRESSURE = "PIDPressure"
    NETWORK_UNAVAILABLE = "NetworkUnavailable"
    NOT_READY = "NotReady"


class AddonStatus(str, Enum):
    """EKS addon health status."""

    ACTIVE = "active"
    DEGRADED = "degraded"
    FAILED = "failed"
    UPDATING = "updating"


class EscalationUrgency(str, Enum):
    """Urgency level for escalation decisions."""

    PAGE_NOW = "page_now"
    NOTIFY_CHANNEL = "notify_channel"
    BUSINESS_HOURS = "business_hours"


class ResourceIdentifierType(str, Enum):
    """Type of resource identifier."""

    ARN = "arn"
    K8S_RESOURCE = "k8s_resource"
    METRIC_DIMENSION = "metric_dimension"


class K8sEventType(str, Enum):
    """Type of Kubernetes event."""

    WARNING = "Warning"
    NORMAL = "Normal"


# --- Base Models ---


class ResourceIdentifier(BaseModel):
    """Identifier for an affected resource."""

    type: ResourceIdentifierType
    value: str
    display_name: str = Field(alias="displayName", default="")

    model_config = {"populate_by_name": True}


class TimeRange(BaseModel):
    """A time range with start and end."""

    start: datetime
    end: datetime


class DataPoint(BaseModel):
    """A single metric data point."""

    timestamp: datetime
    value: float


# --- Core Data Models ---


class NormalizedAlert(BaseModel):
    """Common format for alerts from any source system.

    Maps to the TypeScript NormalizedAlert type in the design document.
    """

    id: str
    source: AlertSource
    source_alarm_id: str = Field(alias="sourceAlarmId", default="")
    severity: Severity
    title: str
    description: str = ""
    state: AlertState = AlertState.FIRING
    fired_at: datetime = Field(alias="firedAt")
    resolved_at: datetime | None = Field(alias="resolvedAt", default=None)

    # Affected resources
    affected_resources: list[ResourceIdentifier] = Field(
        alias="affectedResources", default_factory=list
    )
    region: str
    account: str | None = None

    # Kubernetes context (if applicable)
    cluster: str | None = None
    namespace: str | None = None
    workload: str | None = None

    # Metric context
    metric_name: str | None = Field(alias="metricName", default=None)
    metric_namespace: str | None = Field(alias="metricNamespace", default=None)
    dimensions: dict[str, str] | None = None
    threshold: float | None = None
    current_value: float | None = Field(alias="currentValue", default=None)

    # Raw source data for provenance
    raw_payload: dict[str, Any] = Field(alias="rawPayload", default_factory=dict)

    model_config = {"populate_by_name": True}


class MetricDeviation(BaseModel):
    """Result of baseline comparison for a single metric.

    Maps to the TypeScript MetricDeviation type in the design document.
    """

    metric_name: str = Field(alias="metricName")
    namespace: str
    dimensions: dict[str, str] = Field(default_factory=dict)
    time_range: TimeRange = Field(alias="timeRange")

    # Current values
    current_mean: float = Field(alias="currentMean")
    current_p95: float = Field(alias="currentP95")
    current_max: float = Field(alias="currentMax")

    # Baseline values
    baseline_mean: float = Field(alias="baselineMean")
    baseline_p95: float = Field(alias="baselineP95")
    baseline_p99: float = Field(alias="baselineP99")
    baseline_std_dev: float = Field(alias="baselineStdDev")

    # Analysis
    deviation_factor: float = Field(alias="deviationFactor")
    classification: DeviationClassification
    confidence: float = Field(ge=0.0, le=1.0)
    anomaly_start_time: datetime | None = Field(alias="anomalyStartTime", default=None)

    # Evidence
    data_points: list[DataPoint] = Field(alias="dataPoints", default_factory=list)
    baseline_data_points: list[DataPoint] = Field(
        alias="baselineDataPoints", default_factory=list
    )

    model_config = {"populate_by_name": True}


class Change(BaseModel):
    """A single infrastructure or deployment change.

    Maps to the TypeScript Change type in the design document.
    """

    id: str
    type: ChangeType
    source: ChangeSource
    timestamp: datetime
    actor: str
    description: str
    affected_resource: str = Field(alias="affectedResource")
    details: dict[str, Any] = Field(default_factory=dict)

    # Correlation scoring
    temporal_proximity: float = Field(alias="temporalProximity")
    resource_overlap: bool = Field(alias="resourceOverlap")
    correlation_score: float = Field(alias="correlationScore", ge=0.0, le=1.0)

    model_config = {"populate_by_name": True}


class CorrelatedChanges(BaseModel):
    """Changes discovered in the lookback window.

    Maps to the TypeScript CorrelatedChanges type in the design document.
    """

    changes: list[Change] = Field(default_factory=list)
    lookback_window: str = Field(alias="lookbackWindow")
    affected_resources: list[ResourceIdentifier] = Field(
        alias="affectedResources", default_factory=list
    )

    model_config = {"populate_by_name": True}


class LogFinding(BaseModel):
    """A single log finding (error pattern).

    Maps to the TypeScript LogFinding type in the design document.
    """

    pattern: str
    count: int
    first_seen: datetime = Field(alias="firstSeen")
    last_seen: datetime = Field(alias="lastSeen")
    is_new: bool = Field(alias="isNew")
    severity: LogSeverity
    sample_log_lines: list[str] = Field(alias="sampleLogLines", default_factory=list)
    log_group: str = Field(alias="logGroup")
    log_stream: str | None = Field(alias="logStream", default=None)

    model_config = {"populate_by_name": True}


class LogFindings(BaseModel):
    """Error patterns and anomalies found in logs.

    Maps to the TypeScript LogFindings type in the design document.
    """

    findings: list[LogFinding] = Field(default_factory=list)
    queried_log_groups: list[str] = Field(alias="queriedLogGroups", default_factory=list)
    time_range: TimeRange = Field(alias="timeRange")
    total_error_count: int = Field(alias="totalErrorCount", default=0)
    baseline_error_count: int = Field(alias="baselineErrorCount", default=0)

    model_config = {"populate_by_name": True}


class NodeCondition(BaseModel):
    """A node condition report.

    Maps to the TypeScript NodeCondition type in the design document.
    """

    node_name: str = Field(alias="nodeName")
    condition: NodeConditionType
    status: bool
    since: datetime
    message: str = ""

    model_config = {"populate_by_name": True}


class PodFailure(BaseModel):
    """A pod failure report.

    Maps to the TypeScript PodFailure type in the design document.
    """

    name: str
    namespace: str
    reason: PodFailureReason
    restart_count: int = Field(alias="restartCount", default=0)
    last_transition: datetime = Field(alias="lastTransition")
    message: str = ""

    model_config = {"populate_by_name": True}


class AddonHealth(BaseModel):
    """EKS addon health report.

    Maps to the TypeScript AddonHealth type in the design document.
    """

    name: str
    version: str
    status: AddonStatus
    health: str | None = None
    issues: list[str] | None = None

    model_config = {"populate_by_name": True}


class K8sEventSummary(BaseModel):
    """Summary of a Kubernetes event.

    Maps to the TypeScript K8sEventSummary type in the design document.
    """

    type: K8sEventType
    reason: str
    involved_object: str = Field(alias="involvedObject")
    message: str
    count: int = 1
    first_timestamp: datetime = Field(alias="firstTimestamp")
    last_timestamp: datetime = Field(alias="lastTimestamp")

    model_config = {"populate_by_name": True}


class NodesStatus(BaseModel):
    """Node status summary within a ClusterHealthReport.

    Maps to the TypeScript nodes nested object in ClusterHealthReport.
    """

    total: int = 0
    ready: int = 0
    not_ready: list[str] = Field(alias="notReady", default_factory=list)
    conditions: list[NodeCondition] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class PodsStatus(BaseModel):
    """Pod status summary within a ClusterHealthReport.

    Maps to the TypeScript pods nested object in ClusterHealthReport.
    """

    total: int = 0
    running: int = 0
    pending: int = 0
    failed: int = 0
    crash_looping: list[PodFailure] = Field(alias="crashLooping", default_factory=list)
    oom_killed: list[PodFailure] = Field(alias="oomKilled", default_factory=list)

    model_config = {"populate_by_name": True}


class ClusterHealthReport(BaseModel):
    """Kubernetes cluster state summary.

    Maps to the TypeScript ClusterHealthReport type in the design document.
    """

    cluster_name: str = Field(alias="clusterName")
    region: str
    overall_health: ClusterHealth = Field(alias="overallHealth")

    nodes: NodesStatus = Field(default_factory=NodesStatus)
    pods: PodsStatus = Field(default_factory=PodsStatus)
    addons: list[AddonHealth] = Field(default_factory=list)
    recent_events: list[K8sEventSummary] = Field(
        alias="recentEvents", default_factory=list
    )

    model_config = {"populate_by_name": True}


class EvidenceItem(BaseModel):
    """A single piece of evidence supporting a hypothesis.

    Maps to the TypeScript EvidenceItem type in the design document.
    """

    claim: str
    source: str
    timestamp: datetime | None = None
    weight: float = Field(default=0.5, ge=0.0, le=1.0)
    verification_command: str | None = Field(alias="verificationCommand", default=None)
    console_url: str | None = Field(alias="consoleUrl", default=None)

    model_config = {"populate_by_name": True}


class Hypothesis(BaseModel):
    """A ranked root cause hypothesis.

    Maps to the TypeScript Hypothesis type in the design document.
    """

    rank: int
    title: str
    description: str
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence: list[EvidenceItem] = Field(
        alias="supportingEvidence", default_factory=list
    )
    contradicting_evidence: list[EvidenceItem] = Field(
        alias="contradictingEvidence", default_factory=list
    )
    suggested_verification: list[str] = Field(
        alias="suggestedVerification", default_factory=list
    )

    model_config = {"populate_by_name": True}


class BlastRadius(BaseModel):
    """Quantification of incident impact scope.

    Maps to the TypeScript BlastRadius type in the design document.
    """

    summary: str
    affected_services: list[str] = Field(alias="affectedServices", default_factory=list)
    affected_namespaces: list[str] = Field(
        alias="affectedNamespaces", default_factory=list
    )
    affected_nodes: list[str] = Field(alias="affectedNodes", default_factory=list)
    impacted_pod_count: int = Field(alias="impactedPodCount", default=0)
    region: str

    model_config = {"populate_by_name": True}


class EscalationDecision(BaseModel):
    """Escalation decision output.

    Maps to the TypeScript EscalationDecision type in the design document.
    """

    escalate: bool
    reason: str
    urgency: EscalationUrgency
    suggested_responders: list[str] = Field(
        alias="suggestedResponders", default_factory=list
    )

    model_config = {"populate_by_name": True}


class SlackMessageMetadata(BaseModel):
    """Metadata attached to a Slack message."""

    incident_id: str = Field(alias="incidentId")
    severity: str
    generated_by: str = Field(alias="generatedBy", default="codeblue-ai")

    model_config = {"populate_by_name": True}


class SlackMessage(BaseModel):
    """Structured Slack message.

    Maps to the TypeScript SlackMessage type in the design document.
    """

    channel: str
    text: str
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    metadata: SlackMessageMetadata | None = None

    model_config = {"populate_by_name": True}


class IncidentReport(BaseModel):
    """The final assembled incident report.

    Maps to the TypeScript IncidentReport type in the design document.
    """

    incident_id: str = Field(alias="incidentId")
    generated_at: datetime = Field(alias="generatedAt")
    processing_duration: str = Field(alias="processingDuration")

    # Alert source
    alert: NormalizedAlert

    # Severity assessment
    severity: Severity
    blast_radius: BlastRadius = Field(alias="blastRadius")

    # Diagnosis
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)

    # Evidence
    metric_deviation: MetricDeviation | None = Field(
        alias="metricDeviation", default=None
    )
    correlated_changes: CorrelatedChanges | None = Field(
        alias="correlatedChanges", default=None
    )
    log_findings: LogFindings | None = Field(alias="logFindings", default=None)
    cluster_health: ClusterHealthReport | None = Field(
        alias="clusterHealth", default=None
    )

    # Actions
    recommended_actions: list[str] = Field(
        alias="recommendedActions", default_factory=list
    )
    escalation: EscalationDecision | None = None
    evidence_links: list[EvidenceItem] = Field(
        alias="evidenceLinks", default_factory=list
    )

    # Degradation annotations (Requirement 11.2)
    unavailable_sources: list[str] = Field(
        alias="unavailableSources", default_factory=list
    )
    data_gaps: list[dict[str, str]] = Field(
        alias="dataGaps", default_factory=list
    )

    model_config = {"populate_by_name": True}


# --- Provenance Models ---


class ProvenanceQueryParameters(BaseModel):
    """Query parameters attached to a provenance annotation for reproducibility."""

    time_range: TimeRange | None = Field(alias="timeRange", default=None)
    filters: dict[str, str] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class ProvenanceAnnotatedFinding(BaseModel):
    """A finding annotated with provenance information.

    Maps to the TypeScript ProvenanceAnnotatedFinding type in the design document.
    Each claim links back to its source data with console URLs, kubectl commands,
    and query parameters for reproducibility.
    """

    claim: str
    source: str
    timestamp: datetime | None = None
    provenance_resolved: bool = Field(alias="provenanceResolved", default=False)
    console_url: str | None = Field(alias="consoleUrl", default=None)
    verification_command: str | None = Field(alias="verificationCommand", default=None)
    query_parameters: ProvenanceQueryParameters | None = Field(
        alias="queryParameters", default=None
    )
    unavailability_reason: str | None = Field(alias="unavailabilityReason", default=None)

    model_config = {"populate_by_name": True}


# --- Orchestrator Models ---


class CollectedSignals(BaseModel):
    """All signals collected during an incident workflow.

    Used by the Orchestrator to aggregate data for the hypothesis engine,
    and by TriageSession to accumulate evidence across follow-ups.
    """

    metric_deviation: MetricDeviation | None = Field(alias="metricDeviation", default=None)
    correlated_changes: CorrelatedChanges | None = Field(
        alias="correlatedChanges", default=None
    )
    log_findings: LogFindings | None = Field(alias="logFindings", default=None)
    cluster_health: ClusterHealthReport | None = Field(
        alias="clusterHealth", default=None
    )
    evidence_items: list[EvidenceItem] = Field(
        alias="evidenceItems", default_factory=list
    )
    unavailable_sources: list[str] = Field(
        alias="unavailableSources", default_factory=list
    )

    model_config = {"populate_by_name": True}


class WorkflowState(BaseModel):
    """Orchestrator workflow state for tracking incident processing.

    Maps to the TypeScript WorkflowState type in the design document.
    """

    incident_id: str = Field(alias="incidentId")
    phase: Literal[
        "ingestion",
        "metric_analysis",
        "cluster_check",
        "correlation",
        "log_triage",
        "hypothesis",
        "reporting",
    ]
    started_at: datetime = Field(alias="startedAt")
    signals: CollectedSignals = Field(default_factory=CollectedSignals)
    errors: list["SkillError"] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


# --- Skill Response Models ---


class SkillResponse(BaseModel):
    """Standard skill script response envelope.

    All skill scripts return this structure (Requirement 14.3, 14.8).
    Status is "success" or "error". When status is "error", the message
    field describes what went wrong. The data field contains the skill's
    output on success.
    """

    status: Literal["success", "error"]
    message: str = ""
    data: dict[str, Any] | None = None

    model_config = {"populate_by_name": True}


class SkillError(BaseModel):
    """Structured error from a skill invocation.

    Used by the Orchestrator to track errors in WorkflowState.
    """

    skill_name: str = Field(alias="skillName")
    error_type: str = Field(alias="errorType")
    message: str
    timestamp: datetime
    recoverable: bool = True

    model_config = {"populate_by_name": True}


# --- Interactive Triage Models ---


class ConversationTurn(BaseModel):
    """A single exchange in a triage session.

    Maps to the TypeScript ConversationTurn type in the design document.
    """

    role: Literal["user", "agent"]
    content: str
    timestamp: datetime
    skills_invoked: list[str] = Field(alias="skillsInvoked", default_factory=list)
    evidence_added: list[EvidenceItem] = Field(
        alias="evidenceAdded", default_factory=list
    )

    model_config = {"populate_by_name": True}


class TriageSession(BaseModel):
    """A stateful triage session.

    Maps to the TypeScript TriageSession type in the design document.
    """

    session_id: str = Field(alias="sessionId")
    started_at: datetime = Field(alias="startedAt")
    normalized_alert: NormalizedAlert = Field(alias="normalizedAlert")
    evidence_accumulator: list[EvidenceItem] = Field(
        alias="evidenceAccumulator", default_factory=list
    )
    conversation_history: list[ConversationTurn] = Field(
        alias="conversationHistory", default_factory=list
    )
    active_hypotheses: list[Hypothesis] = Field(
        alias="activeHypotheses", default_factory=list
    )

    model_config = {"populate_by_name": True}


class TriageResponse(BaseModel):
    """Response from a triage interaction.

    Maps to the TypeScript TriageResponse type in the design document.
    """

    summary: str
    new_evidence: list[EvidenceItem] = Field(alias="newEvidence", default_factory=list)
    updated_hypotheses: list[Hypothesis] = Field(
        alias="updatedHypotheses", default_factory=list
    )
    suggested_next_steps: list[str] = Field(
        alias="suggestedNextSteps", default_factory=list
    )

    model_config = {"populate_by_name": True}


# --- Module Exports ---

__all__ = [
    # Enums
    "AlertSource",
    "AlertState",
    "Severity",
    "DeviationClassification",
    "ChangeType",
    "ChangeSource",
    "LogSeverity",
    "ClusterHealth",
    "PodFailureReason",
    "NodeConditionType",
    "AddonStatus",
    "EscalationUrgency",
    "ResourceIdentifierType",
    "K8sEventType",
    # Base Models
    "ResourceIdentifier",
    "TimeRange",
    "DataPoint",
    # Core Data Models
    "NormalizedAlert",
    "MetricDeviation",
    "Change",
    "CorrelatedChanges",
    "LogFinding",
    "LogFindings",
    "NodeCondition",
    "PodFailure",
    "AddonHealth",
    "K8sEventSummary",
    "NodesStatus",
    "PodsStatus",
    "ClusterHealthReport",
    "EvidenceItem",
    "Hypothesis",
    "BlastRadius",
    "EscalationDecision",
    "SlackMessageMetadata",
    "SlackMessage",
    "IncidentReport",
    # Provenance Models
    "ProvenanceQueryParameters",
    "ProvenanceAnnotatedFinding",
    # Orchestrator Models
    "CollectedSignals",
    "WorkflowState",
    # Skill Response Models
    "SkillResponse",
    "SkillError",
    # Interactive Triage Models
    "ConversationTurn",
    "TriageSession",
    "TriageResponse",
]
