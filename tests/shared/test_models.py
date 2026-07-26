"""Tests for shared data models: JSON roundtrip, validation, and completeness.

Validates Requirements 14.3 (structured JSON communication) and 14.8 (validation).
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from skills.shared.models import (
    AddonHealth,
    AddonStatus,
    AlertSource,
    AlertState,
    BlastRadius,
    Change,
    ChangeSource,
    ChangeType,
    ClusterHealth,
    ClusterHealthReport,
    CollectedSignals,
    ConversationTurn,
    CorrelatedChanges,
    DataPoint,
    DeviationClassification,
    EscalationDecision,
    EscalationUrgency,
    EvidenceItem,
    Hypothesis,
    IncidentReport,
    K8sEventSummary,
    K8sEventType,
    LogFinding,
    LogFindings,
    LogSeverity,
    MetricDeviation,
    NodeCondition,
    NodeConditionType,
    NodesStatus,
    NormalizedAlert,
    PodFailure,
    PodFailureReason,
    PodsStatus,
    ProvenanceAnnotatedFinding,
    ProvenanceQueryParameters,
    ResourceIdentifier,
    ResourceIdentifierType,
    Severity,
    SkillError,
    SkillResponse,
    SlackMessage,
    SlackMessageMetadata,
    TimeRange,
    TriageResponse,
    TriageSession,
    WorkflowState,
)


# --- Fixtures ---


NOW = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
EARLIER = datetime(2024, 6, 15, 11, 0, 0, tzinfo=timezone.utc)


def _make_resource_identifier() -> ResourceIdentifier:
    return ResourceIdentifier(
        type=ResourceIdentifierType.ARN,
        value="arn:aws:ec2:us-east-1:123456789:instance/i-abc123",
        display_name="i-abc123",
    )


def _make_normalized_alert() -> NormalizedAlert:
    return NormalizedAlert(
        id="alert-001",
        source=AlertSource.CLOUDWATCH,
        source_alarm_id="arn:aws:cloudwatch:alarm:test",
        severity=Severity.HIGH,
        title="CPU > 90%",
        description="Instance CPU exceeded threshold",
        state=AlertState.FIRING,
        fired_at=EARLIER,
        affected_resources=[_make_resource_identifier()],
        region="us-east-1",
        account="123456789012",
        cluster="prod-cluster",
        namespace="payments",
        workload="payment-service",
        metric_name="CPUUtilization",
        metric_namespace="AWS/EC2",
        dimensions={"InstanceId": "i-abc123"},
        threshold=90.0,
        current_value=95.5,
        raw_payload={"alarm_name": "cpu-high", "state": "ALARM"},
    )


def _make_time_range() -> TimeRange:
    return TimeRange(start=EARLIER, end=NOW)


def _make_metric_deviation() -> MetricDeviation:
    return MetricDeviation(
        metric_name="CPUUtilization",
        namespace="AWS/EC2",
        dimensions={"InstanceId": "i-abc123"},
        time_range=_make_time_range(),
        current_mean=92.5,
        current_p95=97.0,
        current_max=99.8,
        baseline_mean=45.0,
        baseline_p95=65.0,
        baseline_p99=72.0,
        baseline_std_dev=10.0,
        deviation_factor=4.75,
        classification=DeviationClassification.ANOMALOUS,
        confidence=0.85,
        anomaly_start_time=EARLIER,
        data_points=[DataPoint(timestamp=NOW, value=95.0)],
        baseline_data_points=[DataPoint(timestamp=EARLIER, value=45.0)],
    )


def _make_change() -> Change:
    return Change(
        id="change-001",
        type=ChangeType.DEPLOYMENT,
        source=ChangeSource.KUBERNETES,
        timestamp=EARLIER,
        actor="deploy-bot",
        description="Rolled out payment-service v2.3.1",
        affected_resource="deployment/payment-service",
        details={"replicas": 3, "image": "payment:v2.3.1"},
        temporal_proximity=120.0,
        resource_overlap=True,
        correlation_score=0.92,
    )


def _make_correlated_changes() -> CorrelatedChanges:
    return CorrelatedChanges(
        changes=[_make_change()],
        lookback_window="PT24H",
        affected_resources=[_make_resource_identifier()],
    )


def _make_log_finding() -> LogFinding:
    return LogFinding(
        pattern="NullPointerException",
        count=42,
        first_seen=EARLIER,
        last_seen=NOW,
        is_new=True,
        severity=LogSeverity.ERROR,
        sample_log_lines=["NPE at PaymentService.java:42", "NPE at PaymentService.java:55"],
        log_group="/aws/eks/prod/payment-service",
        log_stream="pod-abc123",
    )


def _make_log_findings() -> LogFindings:
    return LogFindings(
        findings=[_make_log_finding()],
        queried_log_groups=["/aws/eks/prod/payment-service"],
        time_range=_make_time_range(),
        total_error_count=42,
        baseline_error_count=3,
    )


def _make_evidence_item() -> EvidenceItem:
    return EvidenceItem(
        claim="CPU spiked after deployment",
        source="metric-baseline",
        timestamp=NOW,
        weight=0.9,
        verification_command="kubectl top pods -n payments",
        console_url="https://console.aws.amazon.com/cloudwatch/...",
    )


def _make_hypothesis() -> Hypothesis:
    return Hypothesis(
        rank=1,
        title="Deployment caused CPU spike",
        description="The v2.3.1 deployment introduced a regression causing high CPU",
        confidence=0.85,
        supporting_evidence=[_make_evidence_item()],
        contradicting_evidence=[],
        suggested_verification=["Roll back to v2.3.0 and observe CPU", "Check thread dump"],
    )


def _make_blast_radius() -> BlastRadius:
    return BlastRadius(
        summary="12 pods across 3 nodes in us-east-1",
        affected_services=["payment-service", "order-service"],
        affected_namespaces=["payments", "orders"],
        affected_nodes=["node-1", "node-2", "node-3"],
        impacted_pod_count=12,
        region="us-east-1",
    )


def _make_escalation_decision() -> EscalationDecision:
    return EscalationDecision(
        escalate=True,
        reason="Critical severity with multi-service blast radius",
        urgency=EscalationUrgency.PAGE_NOW,
        suggested_responders=["payments-oncall", "platform-sre"],
    )


def _make_cluster_health_report() -> ClusterHealthReport:
    return ClusterHealthReport(
        cluster_name="prod-cluster",
        region="us-east-1",
        overall_health=ClusterHealth.DEGRADED,
        nodes=NodesStatus(
            total=5,
            ready=4,
            not_ready=["node-5"],
            conditions=[
                NodeCondition(
                    node_name="node-5",
                    condition=NodeConditionType.MEMORY_PRESSURE,
                    status=True,
                    since=EARLIER,
                    message="Memory pressure detected",
                )
            ],
        ),
        pods=PodsStatus(
            total=30,
            running=28,
            pending=1,
            failed=1,
            crash_looping=[
                PodFailure(
                    name="payment-service-abc123",
                    namespace="payments",
                    reason=PodFailureReason.CRASH_LOOP_BACK_OFF,
                    restart_count=5,
                    last_transition=EARLIER,
                    message="Back-off restarting failed container",
                )
            ],
            oom_killed=[],
        ),
        addons=[
            AddonHealth(
                name="vpc-cni",
                version="1.14.1",
                status=AddonStatus.ACTIVE,
                health="healthy",
                issues=None,
            )
        ],
        recent_events=[
            K8sEventSummary(
                type=K8sEventType.WARNING,
                reason="BackOff",
                involved_object="pod/payment-service-abc123",
                message="Back-off restarting failed container",
                count=5,
                first_timestamp=EARLIER,
                last_timestamp=NOW,
            )
        ],
    )


def _make_slack_message() -> SlackMessage:
    return SlackMessage(
        channel="#incidents",
        text="INCIDENT: CPU spike in payments",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "🔴 *INCIDENT*"}}],
        metadata=SlackMessageMetadata(
            incident_id="inc-001",
            severity="critical",
            generated_by="codeblue-ai",
        ),
    )


def _make_incident_report() -> IncidentReport:
    return IncidentReport(
        incident_id="inc-001",
        generated_at=NOW,
        processing_duration="PT32S",
        alert=_make_normalized_alert(),
        severity=Severity.HIGH,
        blast_radius=_make_blast_radius(),
        hypotheses=[_make_hypothesis()],
        confidence=0.85,
        metric_deviation=_make_metric_deviation(),
        correlated_changes=_make_correlated_changes(),
        log_findings=_make_log_findings(),
        cluster_health=_make_cluster_health_report(),
        recommended_actions=["Roll back deployment", "Check memory limits"],
        escalation=_make_escalation_decision(),
        evidence_links=[_make_evidence_item()],
    )


def _make_conversation_turn() -> ConversationTurn:
    return ConversationTurn(
        role="user",
        content="our prod cluster is throwing 503s",
        timestamp=NOW,
        skills_invoked=["alert-ingestion", "k8s-cluster-health"],
        evidence_added=[_make_evidence_item()],
    )


def _make_triage_session() -> TriageSession:
    return TriageSession(
        session_id="session-001",
        started_at=EARLIER,
        normalized_alert=_make_normalized_alert(),
        evidence_accumulator=[_make_evidence_item()],
        conversation_history=[_make_conversation_turn()],
        active_hypotheses=[_make_hypothesis()],
    )


def _make_triage_response() -> TriageResponse:
    return TriageResponse(
        summary="Found CPU spike correlated with deployment",
        new_evidence=[_make_evidence_item()],
        updated_hypotheses=[_make_hypothesis()],
        suggested_next_steps=["Check pod logs", "Review deployment diff"],
    )


# --- JSON Roundtrip Tests ---


class TestJsonRoundtrip:
    """Test that models serialize to JSON and deserialize back identically."""

    def _roundtrip(self, model_instance: object) -> None:
        """Serialize to JSON, then deserialize, and verify equality."""
        model_class = type(model_instance)
        json_str = model_instance.model_dump_json(by_alias=True)
        restored = model_class.model_validate_json(json_str)
        assert model_instance == restored

    def test_resource_identifier_roundtrip(self):
        self._roundtrip(_make_resource_identifier())

    def test_time_range_roundtrip(self):
        self._roundtrip(_make_time_range())

    def test_data_point_roundtrip(self):
        self._roundtrip(DataPoint(timestamp=NOW, value=42.5))

    def test_normalized_alert_roundtrip(self):
        self._roundtrip(_make_normalized_alert())

    def test_metric_deviation_roundtrip(self):
        self._roundtrip(_make_metric_deviation())

    def test_change_roundtrip(self):
        self._roundtrip(_make_change())

    def test_correlated_changes_roundtrip(self):
        self._roundtrip(_make_correlated_changes())

    def test_log_finding_roundtrip(self):
        self._roundtrip(_make_log_finding())

    def test_log_findings_roundtrip(self):
        self._roundtrip(_make_log_findings())

    def test_cluster_health_report_roundtrip(self):
        self._roundtrip(_make_cluster_health_report())

    def test_evidence_item_roundtrip(self):
        self._roundtrip(_make_evidence_item())

    def test_hypothesis_roundtrip(self):
        self._roundtrip(_make_hypothesis())

    def test_blast_radius_roundtrip(self):
        self._roundtrip(_make_blast_radius())

    def test_escalation_decision_roundtrip(self):
        self._roundtrip(_make_escalation_decision())

    def test_slack_message_roundtrip(self):
        self._roundtrip(_make_slack_message())

    def test_incident_report_roundtrip(self):
        self._roundtrip(_make_incident_report())

    def test_conversation_turn_roundtrip(self):
        self._roundtrip(_make_conversation_turn())

    def test_triage_session_roundtrip(self):
        self._roundtrip(_make_triage_session())

    def test_triage_response_roundtrip(self):
        self._roundtrip(_make_triage_response())


# --- camelCase Serialization Tests ---


class TestCamelCaseSerialization:
    """Verify that models produce camelCase keys when serialized with by_alias=True."""

    def test_normalized_alert_camel_case(self):
        alert = _make_normalized_alert()
        data = alert.model_dump(by_alias=True)
        assert "sourceAlarmId" in data
        assert "firedAt" in data
        assert "affectedResources" in data
        assert "rawPayload" in data
        assert "metricName" in data
        assert "currentValue" in data

    def test_metric_deviation_camel_case(self):
        md = _make_metric_deviation()
        data = md.model_dump(by_alias=True)
        assert "metricName" in data
        assert "timeRange" in data
        assert "currentMean" in data
        assert "baselineMean" in data
        assert "deviationFactor" in data
        assert "anomalyStartTime" in data
        assert "dataPoints" in data
        assert "baselineDataPoints" in data

    def test_cluster_health_report_camel_case(self):
        report = _make_cluster_health_report()
        data = report.model_dump(by_alias=True)
        assert "clusterName" in data
        assert "overallHealth" in data
        assert "recentEvents" in data
        nodes = data["nodes"]
        assert "notReady" in nodes
        pods = data["pods"]
        assert "crashLooping" in pods
        assert "oomKilled" in pods

    def test_hypothesis_camel_case(self):
        h = _make_hypothesis()
        data = h.model_dump(by_alias=True)
        assert "supportingEvidence" in data
        assert "contradictingEvidence" in data
        assert "suggestedVerification" in data

    def test_evidence_item_camel_case(self):
        e = _make_evidence_item()
        data = e.model_dump(by_alias=True)
        assert "verificationCommand" in data
        assert "consoleUrl" in data

    def test_incident_report_camel_case(self):
        report = _make_incident_report()
        data = report.model_dump(by_alias=True)
        assert "incidentId" in data
        assert "generatedAt" in data
        assert "processingDuration" in data
        assert "blastRadius" in data
        assert "metricDeviation" in data
        assert "correlatedChanges" in data
        assert "logFindings" in data
        assert "clusterHealth" in data
        assert "recommendedActions" in data
        assert "evidenceLinks" in data

    def test_triage_session_camel_case(self):
        session = _make_triage_session()
        data = session.model_dump(by_alias=True)
        assert "sessionId" in data
        assert "startedAt" in data
        assert "normalizedAlert" in data
        assert "evidenceAccumulator" in data
        assert "conversationHistory" in data
        assert "activeHypotheses" in data

    def test_triage_response_camel_case(self):
        resp = _make_triage_response()
        data = resp.model_dump(by_alias=True)
        assert "newEvidence" in data
        assert "updatedHypotheses" in data
        assert "suggestedNextSteps" in data


# --- Validation Tests ---


class TestValidation:
    """Test that models reject invalid inputs appropriately."""

    def test_confidence_out_of_range_high(self):
        with pytest.raises(ValidationError):
            MetricDeviation(
                metric_name="test",
                namespace="test",
                time_range=_make_time_range(),
                current_mean=1.0,
                current_p95=1.0,
                current_max=1.0,
                baseline_mean=1.0,
                baseline_p95=1.0,
                baseline_p99=1.0,
                baseline_std_dev=1.0,
                deviation_factor=1.0,
                classification=DeviationClassification.NORMAL,
                confidence=1.5,  # invalid: > 1.0
            )

    def test_confidence_out_of_range_low(self):
        with pytest.raises(ValidationError):
            MetricDeviation(
                metric_name="test",
                namespace="test",
                time_range=_make_time_range(),
                current_mean=1.0,
                current_p95=1.0,
                current_max=1.0,
                baseline_mean=1.0,
                baseline_p95=1.0,
                baseline_p99=1.0,
                baseline_std_dev=1.0,
                deviation_factor=1.0,
                classification=DeviationClassification.NORMAL,
                confidence=-0.1,  # invalid: < 0.0
            )

    def test_correlation_score_out_of_range(self):
        with pytest.raises(ValidationError):
            Change(
                id="change-001",
                type=ChangeType.DEPLOYMENT,
                source=ChangeSource.KUBERNETES,
                timestamp=NOW,
                actor="bot",
                description="test",
                affected_resource="deployment/test",
                temporal_proximity=1.0,
                resource_overlap=True,
                correlation_score=1.5,  # invalid: > 1.0
            )

    def test_hypothesis_confidence_out_of_range(self):
        with pytest.raises(ValidationError):
            Hypothesis(
                rank=1,
                title="test",
                description="test",
                confidence=2.0,  # invalid: > 1.0
            )

    def test_evidence_item_weight_out_of_range(self):
        with pytest.raises(ValidationError):
            EvidenceItem(
                claim="test",
                source="test",
                weight=1.5,  # invalid: > 1.0
            )

    def test_invalid_alert_source(self):
        with pytest.raises(ValidationError):
            NormalizedAlert(
                id="test",
                source="invalid_source",  # not a valid AlertSource
                severity=Severity.HIGH,
                title="test",
                fired_at=NOW,
                region="us-east-1",
            )

    def test_invalid_severity(self):
        with pytest.raises(ValidationError):
            NormalizedAlert(
                id="test",
                source=AlertSource.CLOUDWATCH,
                severity="extreme",  # not a valid Severity
                title="test",
                fired_at=NOW,
                region="us-east-1",
            )

    def test_invalid_deviation_classification(self):
        with pytest.raises(ValidationError):
            MetricDeviation(
                metric_name="test",
                namespace="test",
                time_range=_make_time_range(),
                current_mean=1.0,
                current_p95=1.0,
                current_max=1.0,
                baseline_mean=1.0,
                baseline_p95=1.0,
                baseline_p99=1.0,
                baseline_std_dev=1.0,
                deviation_factor=1.0,
                classification="bad_value",  # invalid
                confidence=0.5,
            )

    def test_invalid_escalation_urgency(self):
        with pytest.raises(ValidationError):
            EscalationDecision(
                escalate=True,
                reason="test",
                urgency="immediately",  # invalid
            )

    def test_incident_report_confidence_validation(self):
        with pytest.raises(ValidationError):
            IncidentReport(
                incident_id="test",
                generated_at=NOW,
                processing_duration="PT1S",
                alert=_make_normalized_alert(),
                severity=Severity.HIGH,
                blast_radius=_make_blast_radius(),
                confidence=5.0,  # invalid
            )

    def test_missing_required_fields(self):
        """NormalizedAlert requires id, source, severity, title, fired_at, region."""
        with pytest.raises(ValidationError):
            NormalizedAlert(
                id="test",
                # missing source, severity, title, fired_at, region
            )


# --- Deserialization from camelCase JSON ---


class TestDeserializationFromCamelCase:
    """Test that models can deserialize from camelCase JSON (e.g., from TypeScript)."""

    def test_normalized_alert_from_camel_case_json(self):
        import json

        data = {
            "id": "alert-002",
            "source": "grafana",
            "sourceAlarmId": "grafana-rule-123",
            "severity": "medium",
            "title": "High latency",
            "description": "P99 latency exceeded 500ms",
            "state": "firing",
            "firedAt": "2024-06-15T12:00:00Z",
            "affectedResources": [
                {"type": "k8s_resource", "value": "ns/api-gateway", "displayName": "api-gateway"}
            ],
            "region": "eu-west-1",
            "metricName": "latency_p99",
            "rawPayload": {"rule_id": 123},
        }
        alert = NormalizedAlert.model_validate(data)
        assert alert.id == "alert-002"
        assert alert.source == AlertSource.GRAFANA
        assert alert.source_alarm_id == "grafana-rule-123"
        assert alert.severity == Severity.MEDIUM
        assert alert.affected_resources[0].type == ResourceIdentifierType.K8S_RESOURCE
        assert alert.metric_name == "latency_p99"

    def test_cluster_health_report_from_camel_case_json(self):
        data = {
            "clusterName": "staging-cluster",
            "region": "us-west-2",
            "overallHealth": "healthy",
            "nodes": {
                "total": 3,
                "ready": 3,
                "notReady": [],
                "conditions": [],
            },
            "pods": {
                "total": 20,
                "running": 20,
                "pending": 0,
                "failed": 0,
                "crashLooping": [],
                "oomKilled": [],
            },
            "addons": [
                {"name": "coredns", "version": "1.10.1", "status": "active"}
            ],
            "recentEvents": [],
        }
        report = ClusterHealthReport.model_validate(data)
        assert report.cluster_name == "staging-cluster"
        assert report.overall_health == ClusterHealth.HEALTHY
        assert report.nodes.total == 3
        assert report.nodes.ready == 3
        assert report.pods.total == 20
        assert report.pods.running == 20
        assert len(report.addons) == 1
        assert report.addons[0].status == AddonStatus.ACTIVE

    def test_hypothesis_from_camel_case_json(self):
        data = {
            "rank": 1,
            "title": "Memory leak in v2.3",
            "description": "The latest deploy introduced a memory leak",
            "confidence": 0.75,
            "supportingEvidence": [
                {"claim": "OOM events started after deploy", "source": "log-triage", "weight": 0.8}
            ],
            "contradictingEvidence": [],
            "suggestedVerification": ["Check heap dump", "Compare memory profiles"],
        }
        h = Hypothesis.model_validate(data)
        assert h.rank == 1
        assert h.confidence == 0.75
        assert len(h.supporting_evidence) == 1
        assert h.supporting_evidence[0].weight == 0.8
        assert len(h.suggested_verification) == 2


# --- Module Exports Test ---


class TestModuleExports:
    """Test that __all__ is defined and exports all expected models."""

    def test_all_exports_defined(self):
        from skills.shared import models

        assert hasattr(models, "__all__")
        assert len(models.__all__) > 0

    def test_key_models_in_exports(self):
        from skills.shared.models import __all__

        expected = [
            "NormalizedAlert",
            "MetricDeviation",
            "CorrelatedChanges",
            "LogFindings",
            "ClusterHealthReport",
            "IncidentReport",
            "BlastRadius",
            "Hypothesis",
            "EvidenceItem",
            "EscalationDecision",
            "SlackMessage",
            "TriageSession",
            "TriageResponse",
            "SkillResponse",
            "SkillError",
        ]
        for name in expected:
            assert name in __all__, f"{name} not found in __all__"


# --- SkillResponse and SkillError Tests ---


class TestSkillResponse:
    """Test SkillResponse model for Requirements 14.3 and 14.8."""

    def test_success_response(self):
        resp = SkillResponse(status="success", data={"result": "ok"})
        assert resp.status == "success"
        assert resp.data == {"result": "ok"}
        assert resp.message == ""

    def test_error_response(self):
        resp = SkillResponse(status="error", message="Invalid input: missing 'region' field")
        assert resp.status == "error"
        assert "missing" in resp.message
        assert resp.data is None

    def test_invalid_status(self):
        with pytest.raises(ValidationError):
            SkillResponse(status="partial")  # invalid: not "success" or "error"

    def test_roundtrip(self):
        resp = SkillResponse(status="success", message="", data={"key": "value"})
        json_str = resp.model_dump_json(by_alias=True)
        restored = SkillResponse.model_validate_json(json_str)
        assert resp == restored


class TestSkillError:
    """Test SkillError model for workflow error tracking."""

    def test_skill_error_creation(self):
        err = SkillError(
            skill_name="metric-baseline",
            error_type="timeout",
            message="Skill exceeded 15s timeout",
            timestamp=NOW,
            recoverable=True,
        )
        assert err.skill_name == "metric-baseline"
        assert err.error_type == "timeout"
        assert err.recoverable is True

    def test_skill_error_camel_case(self):
        err = SkillError(
            skill_name="log-triage",
            error_type="connection_error",
            message="CloudWatch MCP unreachable",
            timestamp=NOW,
        )
        data = err.model_dump(by_alias=True)
        assert "skillName" in data
        assert "errorType" in data

    def test_skill_error_roundtrip(self):
        err = SkillError(
            skill_name="deploy-correlation",
            error_type="permission_denied",
            message="CloudTrail access denied",
            timestamp=NOW,
            recoverable=False,
        )
        json_str = err.model_dump_json(by_alias=True)
        restored = SkillError.model_validate_json(json_str)
        assert err == restored


# --- ConversationTurn Role Validation ---


class TestConversationTurnRoleValidation:
    """Test that ConversationTurn.role is constrained to 'user' or 'agent'."""

    def test_valid_user_role(self):
        turn = ConversationTurn(
            role="user",
            content="Check the cluster",
            timestamp=NOW,
        )
        assert turn.role == "user"

    def test_valid_agent_role(self):
        turn = ConversationTurn(
            role="agent",
            content="Checking cluster health...",
            timestamp=NOW,
        )
        assert turn.role == "agent"

    def test_invalid_role(self):
        with pytest.raises(ValidationError):
            ConversationTurn(
                role="system",  # invalid: not "user" or "agent"
                content="test",
                timestamp=NOW,
            )


# --- ProvenanceAnnotatedFinding Tests ---


class TestProvenanceAnnotatedFinding:
    """Test ProvenanceAnnotatedFinding model for evidence provenance tracking."""

    def test_resolved_finding(self):
        finding = ProvenanceAnnotatedFinding(
            claim="CPU spiked to 95% after deployment",
            source="cloudwatch",
            timestamp=NOW,
            provenance_resolved=True,
            console_url="https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#metricsV2:graph=...",
            verification_command=None,
            query_parameters=ProvenanceQueryParameters(
                time_range=_make_time_range(),
                filters={"InstanceId": "i-abc123"},
            ),
        )
        assert finding.provenance_resolved is True
        assert finding.console_url is not None
        assert finding.unavailability_reason is None

    def test_unresolved_finding(self):
        finding = ProvenanceAnnotatedFinding(
            claim="Pod restarted 5 times",
            source="unknown_system",
            timestamp=NOW,
            provenance_resolved=False,
            unavailability_reason="Unrecognized source system: unknown_system",
        )
        assert finding.provenance_resolved is False
        assert finding.console_url is None
        assert "Unrecognized" in finding.unavailability_reason

    def test_camel_case_serialization(self):
        finding = ProvenanceAnnotatedFinding(
            claim="test",
            source="cloudwatch",
            provenance_resolved=True,
            console_url="https://example.com",
            verification_command="kubectl get pods",
        )
        data = finding.model_dump(by_alias=True)
        assert "provenanceResolved" in data
        assert "consoleUrl" in data
        assert "verificationCommand" in data
        assert "queryParameters" in data
        assert "unavailabilityReason" in data

    def test_roundtrip(self):
        finding = ProvenanceAnnotatedFinding(
            claim="Deploy correlation found",
            source="cloudtrail",
            timestamp=NOW,
            provenance_resolved=True,
            console_url="https://console.aws.amazon.com/cloudtrail/...",
            query_parameters=ProvenanceQueryParameters(
                time_range=_make_time_range(),
                filters={"eventName": "UpdateFunctionCode"},
            ),
        )
        json_str = finding.model_dump_json(by_alias=True)
        restored = ProvenanceAnnotatedFinding.model_validate_json(json_str)
        assert finding == restored

    def test_deserialization_from_camel_case(self):
        data = {
            "claim": "Node went NotReady",
            "source": "kubernetes",
            "timestamp": "2024-06-15T12:00:00Z",
            "provenanceResolved": True,
            "consoleUrl": None,
            "verificationCommand": "kubectl describe node node-5",
            "queryParameters": {
                "timeRange": {
                    "start": "2024-06-15T11:00:00Z",
                    "end": "2024-06-15T12:00:00Z",
                },
                "filters": {"nodeName": "node-5"},
            },
            "unavailabilityReason": None,
        }
        finding = ProvenanceAnnotatedFinding.model_validate(data)
        assert finding.claim == "Node went NotReady"
        assert finding.provenance_resolved is True
        assert finding.verification_command == "kubectl describe node node-5"
        assert finding.query_parameters.filters["nodeName"] == "node-5"


# --- CollectedSignals Tests ---


class TestCollectedSignals:
    """Test CollectedSignals model for orchestrator signal aggregation."""

    def test_empty_signals(self):
        signals = CollectedSignals()
        assert signals.metric_deviation is None
        assert signals.correlated_changes is None
        assert signals.log_findings is None
        assert signals.cluster_health is None
        assert signals.evidence_items == []
        assert signals.unavailable_sources == []

    def test_partial_signals(self):
        signals = CollectedSignals(
            metric_deviation=_make_metric_deviation(),
            log_findings=_make_log_findings(),
            unavailable_sources=["kubernetes-mcp", "cloudtrail"],
        )
        assert signals.metric_deviation is not None
        assert signals.correlated_changes is None
        assert signals.log_findings is not None
        assert len(signals.unavailable_sources) == 2

    def test_full_signals(self):
        signals = CollectedSignals(
            metric_deviation=_make_metric_deviation(),
            correlated_changes=_make_correlated_changes(),
            log_findings=_make_log_findings(),
            cluster_health=_make_cluster_health_report(),
            evidence_items=[_make_evidence_item()],
        )
        assert signals.metric_deviation is not None
        assert signals.correlated_changes is not None
        assert signals.log_findings is not None
        assert signals.cluster_health is not None
        assert len(signals.evidence_items) == 1

    def test_camel_case_serialization(self):
        signals = CollectedSignals(
            metric_deviation=_make_metric_deviation(),
            unavailable_sources=["grafana-mcp"],
        )
        data = signals.model_dump(by_alias=True)
        assert "metricDeviation" in data
        assert "correlatedChanges" in data
        assert "logFindings" in data
        assert "clusterHealth" in data
        assert "evidenceItems" in data
        assert "unavailableSources" in data

    def test_roundtrip(self):
        signals = CollectedSignals(
            metric_deviation=_make_metric_deviation(),
            correlated_changes=_make_correlated_changes(),
            log_findings=_make_log_findings(),
            evidence_items=[_make_evidence_item()],
            unavailable_sources=["kubernetes-mcp"],
        )
        json_str = signals.model_dump_json(by_alias=True)
        restored = CollectedSignals.model_validate_json(json_str)
        assert signals == restored


# --- WorkflowState Tests ---


class TestWorkflowState:
    """Test WorkflowState model for orchestrator state tracking."""

    def test_initial_state(self):
        state = WorkflowState(
            incident_id="inc-001",
            phase="ingestion",
            started_at=EARLIER,
        )
        assert state.incident_id == "inc-001"
        assert state.phase == "ingestion"
        assert state.signals is not None
        assert state.errors == []

    def test_all_phases_valid(self):
        valid_phases = [
            "ingestion",
            "metric_analysis",
            "cluster_check",
            "correlation",
            "log_triage",
            "hypothesis",
            "reporting",
        ]
        for phase in valid_phases:
            state = WorkflowState(
                incident_id="inc-001",
                phase=phase,
                started_at=EARLIER,
            )
            assert state.phase == phase

    def test_invalid_phase(self):
        with pytest.raises(ValidationError):
            WorkflowState(
                incident_id="inc-001",
                phase="unknown_phase",
                started_at=EARLIER,
            )

    def test_with_errors(self):
        err = SkillError(
            skill_name="log-triage",
            error_type="timeout",
            message="Exceeded 15s budget",
            timestamp=NOW,
        )
        state = WorkflowState(
            incident_id="inc-001",
            phase="hypothesis",
            started_at=EARLIER,
            errors=[err],
        )
        assert len(state.errors) == 1
        assert state.errors[0].skill_name == "log-triage"

    def test_camel_case_serialization(self):
        state = WorkflowState(
            incident_id="inc-001",
            phase="correlation",
            started_at=EARLIER,
            signals=CollectedSignals(
                metric_deviation=_make_metric_deviation(),
            ),
        )
        data = state.model_dump(by_alias=True)
        assert "incidentId" in data
        assert "startedAt" in data

    def test_roundtrip(self):
        state = WorkflowState(
            incident_id="inc-001",
            phase="hypothesis",
            started_at=EARLIER,
            signals=CollectedSignals(
                metric_deviation=_make_metric_deviation(),
                unavailable_sources=["kubernetes-mcp"],
            ),
            errors=[
                SkillError(
                    skill_name="k8s-cluster-health",
                    error_type="connection_error",
                    message="MCP unreachable",
                    timestamp=NOW,
                    recoverable=True,
                )
            ],
        )
        json_str = state.model_dump_json(by_alias=True)
        restored = WorkflowState.model_validate_json(json_str)
        assert state == restored


# --- Updated Module Exports Test ---


class TestNewModuleExports:
    """Test that new models are included in __all__ exports."""

    def test_new_models_in_exports(self):
        from skills.shared.models import __all__

        new_models = [
            "ProvenanceQueryParameters",
            "ProvenanceAnnotatedFinding",
            "CollectedSignals",
            "WorkflowState",
        ]
        for name in new_models:
            assert name in __all__, f"{name} not found in __all__"
