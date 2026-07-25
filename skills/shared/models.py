"""Shared data models for CodeBlue AI skills.

These are placeholder stubs. Full implementations will be added in task 1.2.
All models follow the TypeScript interfaces defined in the design document,
translated to Python using Pydantic for validation and serialization.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

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


# --- Core Data Models (stubs) ---


class NormalizedAlert(BaseModel):
    """Common format for alerts from any source system."""

    id: str
    source: AlertSource
    source_alarm_id: str = Field(alias="sourceAlarmId", default="")
    severity: Severity
    title: str
    description: str = ""
    state: AlertState = AlertState.FIRING
    fired_at: datetime = Field(alias="firedAt")
    resolved_at: datetime | None = Field(alias="resolvedAt", default=None)

    affected_resources: list[ResourceIdentifier] = Field(
        alias="affectedResources", default_factory=list
    )
    region: str
    account: str | None = None

    cluster: str | None = None
    namespace: str | None = None
    workload: str | None = None

    metric_name: str | None = Field(alias="metricName", default=None)
    metric_namespace: str | None = Field(alias="metricNamespace", default=None)
    dimensions: dict[str, str] | None = None
    threshold: float | None = None
    current_value: float | None = Field(alias="currentValue", default=None)

    raw_payload: dict[str, Any] = Field(alias="rawPayload", default_factory=dict)

    model_config = {"populate_by_name": True}


class MetricDeviation(BaseModel):
    """Result of baseline comparison for a single metric."""

    metric_name: str = Field(alias="metricName")
    namespace: str
    dimensions: dict[str, str] = Field(default_factory=dict)
    time_range: TimeRange = Field(alias="timeRange")

    current_mean: float = Field(alias="currentMean")
    current_p95: float = Field(alias="currentP95")
    current_max: float = Field(alias="currentMax")

    baseline_mean: float = Field(alias="baselineMean")
    baseline_p95: float = Field(alias="baselineP95")
    baseline_p99: float = Field(alias="baselineP99")
    baseline_std_dev: float = Field(alias="baselineStdDev")

    deviation_factor: float = Field(alias="deviationFactor")
    classification: DeviationClassification
    confidence: float = Field(ge=0.0, le=1.0)
    anomaly_start_time: datetime | None = Field(alias="anomalyStartTime", default=None)

    data_points: list[DataPoint] = Field(alias="dataPoints", default_factory=list)
    baseline_data_points: list[DataPoint] = Field(
        alias="baselineDataPoints", default_factory=list
    )

    model_config = {"populate_by_name": True}


class Change(BaseModel):
    """A single infrastructure or deployment change."""

    id: str
    type: ChangeType
    source: ChangeSource
    timestamp: datetime
    actor: str
    description: str
    affected_resource: str = Field(alias="affectedResource")
    details: dict[str, Any] = Field(default_factory=dict)

    temporal_proximity: float = Field(alias="temporalProximity")
    resource_overlap: bool = Field(alias="resourceOverlap")
    correlation_score: float = Field(alias="correlationScore", ge=0.0, le=1.0)

    model_config = {"populate_by_name": True}


class CorrelatedChanges(BaseModel):
    """Changes discovered in the lookback window."""

    changes: list[Change] = Field(default_factory=list)
    lookback_window: str = Field(alias="lookbackWindow")
    affected_resources: list[ResourceIdentifier] = Field(
        alias="affectedResources", default_factory=list
    )

    model_config = {"populate_by_name": True}


class LogFinding(BaseModel):
    """A single log finding (error pattern)."""

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
    """Error patterns and anomalies found in logs."""

    findings: list[LogFinding] = Field(default_factory=list)
    queried_log_groups: list[str] = Field(alias="queriedLogGroups", default_factory=list)
    time_range: TimeRange = Field(alias="timeRange")
    total_error_count: int = Field(alias="totalErrorCount", default=0)
    baseline_error_count: int = Field(alias="baselineErrorCount", default=0)

    model_config = {"populate_by_name": True}


class NodeCondition(BaseModel):
    """A node condition report."""

    node_name: str = Field(alias="nodeName")
    condition: NodeConditionType
    status: bool
    since: datetime
    message: str = ""

    model_config = {"populate_by_name": True}


class PodFailure(BaseModel):
    """A pod failure report."""

    name: str
    namespace: str
    reason: PodFailureReason
    restart_count: int = Field(alias="restartCount", default=0)
    last_transition: datetime = Field(alias="lastTransition")
    message: str = ""

    model_config = {"populate_by_name": True}


class AddonHealth(BaseModel):
    """EKS addon health report."""

    name: str
    version: str
    status: AddonStatus
    health: str | None = None
    issues: list[str] | None = None

    model_config = {"populate_by_name": True}


class K8sEventSummary(BaseModel):
    """Summary of a Kubernetes event."""

    type: str
    reason: str
    involved_object: str = Field(alias="involvedObject")
    message: str
    count: int = 1
    first_timestamp: datetime = Field(alias="firstTimestamp")
    last_timestamp: datetime = Field(alias="lastTimestamp")

    model_config = {"populate_by_name": True}


class ClusterHealthReport(BaseModel):
    """Kubernetes cluster state summary."""

    cluster_name: str = Field(alias="clusterName")
    region: str
    overall_health: ClusterHealth = Field(alias="overallHealth")

    nodes: dict[str, Any] = Field(default_factory=dict)
    pods: dict[str, Any] = Field(default_factory=dict)
    addons: list[AddonHealth] = Field(default_factory=list)
    recent_events: list[K8sEventSummary] = Field(
        alias="recentEvents", default_factory=list
    )

    model_config = {"populate_by_name": True}


class EvidenceItem(BaseModel):
    """A single piece of evidence supporting a hypothesis."""

    claim: str
    source: str
    timestamp: datetime | None = None
    verification_command: str | None = Field(alias="verificationCommand", default=None)
    console_url: str | None = Field(alias="consoleUrl", default=None)

    model_config = {"populate_by_name": True}


class Hypothesis(BaseModel):
    """A ranked root cause hypothesis."""

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
    verification_steps: list[str] = Field(
        alias="verificationSteps", default_factory=list
    )

    model_config = {"populate_by_name": True}


class BlastRadius(BaseModel):
    """Quantification of incident impact scope."""

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
    """Escalation decision output."""

    escalate: bool
    reason: str
    urgency: EscalationUrgency
    suggested_responders: list[str] = Field(
        alias="suggestedResponders", default_factory=list
    )

    model_config = {"populate_by_name": True}


class SlackMessage(BaseModel):
    """Structured Slack message."""

    channel: str
    text: str
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IncidentReport(BaseModel):
    """The final assembled incident report."""

    incident_id: str = Field(alias="incidentId")
    generated_at: datetime = Field(alias="generatedAt")
    processing_duration_ms: int = Field(alias="processingDurationMs")

    alert: NormalizedAlert
    severity: Severity
    blast_radius: BlastRadius = Field(alias="blastRadius")

    hypotheses: list[Hypothesis] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)

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

    recommended_actions: list[str] = Field(
        alias="recommendedActions", default_factory=list
    )
    escalation: EscalationDecision | None = None
    evidence_links: list[EvidenceItem] = Field(
        alias="evidenceLinks", default_factory=list
    )

    model_config = {"populate_by_name": True}


# --- Interactive Triage Models ---


class ConversationTurn(BaseModel):
    """A single exchange in a triage session."""

    role: str  # "user" or "agent"
    content: str
    timestamp: datetime
    skills_invoked: list[str] = Field(alias="skillsInvoked", default_factory=list)
    evidence_added: list[EvidenceItem] = Field(
        alias="evidenceAdded", default_factory=list
    )

    model_config = {"populate_by_name": True}


class TriageSession(BaseModel):
    """A stateful triage session."""

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
    """Response from a triage interaction."""

    summary: str
    new_evidence: list[EvidenceItem] = Field(alias="newEvidence", default_factory=list)
    updated_hypotheses: list[Hypothesis] = Field(
        alias="updatedHypotheses", default_factory=list
    )
    suggested_next_steps: list[str] = Field(
        alias="suggestedNextSteps", default_factory=list
    )

    model_config = {"populate_by_name": True}
