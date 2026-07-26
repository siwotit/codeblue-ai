"""Property-based tests for shared data models (Property 26).

**Validates: Requirements 14.3, 14.8, 14.9**

Property 26: SkillScript input/output contract conformance
- All models serialize to valid JSON and deserialize back identically (roundtrip)
- Invalid inputs produce structured error responses (ValidationError), never unhandled exceptions
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

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
)


# --- Hypothesis Strategies ---

# Constrained datetime strategy (avoid extremes that break JSON serialization)
reasonable_datetimes = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2030, 12, 31),
    timezones=st.just(timezone.utc),
)

# Non-empty text without null bytes (valid for JSON)
safe_text = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),  # exclude surrogates
        blacklist_characters=("\x00",),
    ),
    min_size=1,
    max_size=100,
)

# Bounded floats for confidence/score fields
unit_floats = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)

# Positive floats for metric values
positive_floats = st.floats(min_value=0.0, max_value=1e10, allow_nan=False, allow_infinity=False)

# Enum strategies
alert_sources = st.sampled_from(list(AlertSource))
alert_states = st.sampled_from(list(AlertState))
severities = st.sampled_from(list(Severity))
deviation_classifications = st.sampled_from(list(DeviationClassification))
change_types = st.sampled_from(list(ChangeType))
change_sources = st.sampled_from(list(ChangeSource))
log_severities = st.sampled_from(list(LogSeverity))
cluster_healths = st.sampled_from(list(ClusterHealth))
pod_failure_reasons = st.sampled_from(list(PodFailureReason))
node_condition_types = st.sampled_from(list(NodeConditionType))
addon_statuses = st.sampled_from(list(AddonStatus))
escalation_urgencies = st.sampled_from(list(EscalationUrgency))
resource_id_types = st.sampled_from(list(ResourceIdentifierType))
k8s_event_types = st.sampled_from(list(K8sEventType))


# --- Composite Strategies ---


@st.composite
def resource_identifiers(draw):
    return ResourceIdentifier(
        type=draw(resource_id_types),
        value=draw(safe_text),
        display_name=draw(safe_text),
    )


@st.composite
def time_ranges(draw):
    start = draw(reasonable_datetimes)
    end = draw(reasonable_datetimes.filter(lambda d: d >= start))
    return TimeRange(start=start, end=end)


@st.composite
def data_points(draw):
    return DataPoint(
        timestamp=draw(reasonable_datetimes),
        value=draw(st.floats(allow_nan=False, allow_infinity=False)),
    )


@st.composite
def evidence_items(draw):
    return EvidenceItem(
        claim=draw(safe_text),
        source=draw(safe_text),
        timestamp=draw(st.one_of(st.none(), reasonable_datetimes)),
        weight=draw(unit_floats),
        verification_command=draw(st.one_of(st.none(), safe_text)),
        console_url=draw(st.one_of(st.none(), safe_text)),
    )


@st.composite
def normalized_alerts(draw):
    fired_at = draw(reasonable_datetimes)
    resolved_at = draw(st.one_of(
        st.none(),
        reasonable_datetimes.filter(lambda d: d >= fired_at),
    ))
    return NormalizedAlert(
        id=draw(safe_text),
        source=draw(alert_sources),
        source_alarm_id=draw(safe_text),
        severity=draw(severities),
        title=draw(safe_text),
        description=draw(safe_text),
        state=draw(alert_states),
        fired_at=fired_at,
        resolved_at=resolved_at,
        affected_resources=draw(st.lists(resource_identifiers(), max_size=3)),
        region=draw(safe_text),
        account=draw(st.one_of(st.none(), safe_text)),
        cluster=draw(st.one_of(st.none(), safe_text)),
        namespace=draw(st.one_of(st.none(), safe_text)),
        workload=draw(st.one_of(st.none(), safe_text)),
        metric_name=draw(st.one_of(st.none(), safe_text)),
        metric_namespace=draw(st.one_of(st.none(), safe_text)),
        dimensions=draw(st.one_of(st.none(), st.dictionaries(safe_text, safe_text, max_size=3))),
        threshold=draw(st.one_of(st.none(), positive_floats)),
        current_value=draw(st.one_of(st.none(), positive_floats)),
        raw_payload=draw(st.dictionaries(safe_text, safe_text, max_size=3)),
    )


@st.composite
def metric_deviations(draw):
    return MetricDeviation(
        metric_name=draw(safe_text),
        namespace=draw(safe_text),
        dimensions=draw(st.dictionaries(safe_text, safe_text, max_size=3)),
        time_range=draw(time_ranges()),
        current_mean=draw(positive_floats),
        current_p95=draw(positive_floats),
        current_max=draw(positive_floats),
        baseline_mean=draw(positive_floats),
        baseline_p95=draw(positive_floats),
        baseline_p99=draw(positive_floats),
        baseline_std_dev=draw(positive_floats),
        deviation_factor=draw(positive_floats),
        classification=draw(deviation_classifications),
        confidence=draw(unit_floats),
        anomaly_start_time=draw(st.one_of(st.none(), reasonable_datetimes)),
        data_points=draw(st.lists(data_points(), max_size=5)),
        baseline_data_points=draw(st.lists(data_points(), max_size=5)),
    )


@st.composite
def changes(draw):
    return Change(
        id=draw(safe_text),
        type=draw(change_types),
        source=draw(change_sources),
        timestamp=draw(reasonable_datetimes),
        actor=draw(safe_text),
        description=draw(safe_text),
        affected_resource=draw(safe_text),
        details=draw(st.dictionaries(safe_text, safe_text, max_size=3)),
        temporal_proximity=draw(positive_floats),
        resource_overlap=draw(st.booleans()),
        correlation_score=draw(unit_floats),
    )


@st.composite
def log_findings_st(draw):
    tr = draw(time_ranges())
    return LogFindings(
        findings=draw(st.lists(
            st.builds(
                LogFinding,
                pattern=safe_text,
                count=st.integers(min_value=0, max_value=10000),
                first_seen=reasonable_datetimes,
                last_seen=reasonable_datetimes,
                is_new=st.booleans(),
                severity=log_severities,
                sample_log_lines=st.lists(safe_text, max_size=5),
                log_group=safe_text,
                log_stream=st.one_of(st.none(), safe_text),
            ),
            max_size=3,
        )),
        queried_log_groups=draw(st.lists(safe_text, max_size=3)),
        time_range=tr,
        total_error_count=draw(st.integers(min_value=0, max_value=100000)),
        baseline_error_count=draw(st.integers(min_value=0, max_value=100000)),
    )


@st.composite
def hypotheses(draw):
    return Hypothesis(
        rank=draw(st.integers(min_value=1, max_value=10)),
        title=draw(safe_text),
        description=draw(safe_text),
        confidence=draw(unit_floats),
        supporting_evidence=draw(st.lists(evidence_items(), max_size=3)),
        contradicting_evidence=draw(st.lists(evidence_items(), max_size=2)),
        suggested_verification=draw(st.lists(safe_text, max_size=3)),
    )


@st.composite
def blast_radii(draw):
    return BlastRadius(
        summary=draw(safe_text),
        affected_services=draw(st.lists(safe_text, max_size=3)),
        affected_namespaces=draw(st.lists(safe_text, max_size=3)),
        affected_nodes=draw(st.lists(safe_text, max_size=3)),
        impacted_pod_count=draw(st.integers(min_value=0, max_value=1000)),
        region=draw(safe_text),
    )


@st.composite
def escalation_decisions(draw):
    return EscalationDecision(
        escalate=draw(st.booleans()),
        reason=draw(safe_text),
        urgency=draw(escalation_urgencies),
        suggested_responders=draw(st.lists(safe_text, max_size=3)),
    )


@st.composite
def skill_responses(draw):
    status = draw(st.sampled_from(["success", "error"]))
    return SkillResponse(
        status=status,
        message=draw(safe_text),
        data=draw(st.one_of(st.none(), st.dictionaries(safe_text, safe_text, max_size=3))),
    )


@st.composite
def skill_errors(draw):
    return SkillError(
        skill_name=draw(safe_text),
        error_type=draw(safe_text),
        message=draw(safe_text),
        timestamp=draw(reasonable_datetimes),
        recoverable=draw(st.booleans()),
    )


@st.composite
def slack_messages(draw):
    return SlackMessage(
        channel=draw(safe_text),
        text=draw(safe_text),
        blocks=draw(st.just([])),  # Keep simple for property testing
        metadata=draw(st.one_of(
            st.none(),
            st.builds(
                SlackMessageMetadata,
                incident_id=safe_text,
                severity=safe_text,
                generated_by=safe_text,
            ),
        )),
    )


@st.composite
def conversation_turns(draw):
    return ConversationTurn(
        role=draw(st.sampled_from(["user", "agent"])),
        content=draw(safe_text),
        timestamp=draw(reasonable_datetimes),
        skills_invoked=draw(st.lists(safe_text, max_size=3)),
        evidence_added=draw(st.lists(evidence_items(), max_size=2)),
    )


@st.composite
def triage_responses(draw):
    return TriageResponse(
        summary=draw(safe_text),
        new_evidence=draw(st.lists(evidence_items(), max_size=3)),
        updated_hypotheses=draw(st.lists(hypotheses(), max_size=2)),
        suggested_next_steps=draw(st.lists(safe_text, max_size=3)),
    )


# --- Property Tests: JSON Roundtrip ---


class TestPropertyJsonRoundtrip:
    """Property 26 (Part 1): All models serialize to valid JSON and deserialize back identically.

    **Validates: Requirements 14.3, 14.8, 14.9**
    """

    def _assert_roundtrip(self, instance: BaseModel) -> None:
        """Verify serialize → deserialize produces identical model."""
        model_class = type(instance)
        # Serialize to JSON string (using aliases for camelCase)
        json_str = instance.model_dump_json(by_alias=True)
        # Verify it's valid JSON
        parsed = json.loads(json_str)
        assert isinstance(parsed, dict)
        # Deserialize back and check equality
        restored = model_class.model_validate_json(json_str)
        assert instance == restored

    @given(resource_identifiers())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_resource_identifier_roundtrip(self, instance):
        self._assert_roundtrip(instance)

    @given(time_ranges())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_time_range_roundtrip(self, instance):
        self._assert_roundtrip(instance)

    @given(data_points())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_data_point_roundtrip(self, instance):
        self._assert_roundtrip(instance)

    @given(normalized_alerts())
    @settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
    def test_normalized_alert_roundtrip(self, instance):
        self._assert_roundtrip(instance)

    @given(metric_deviations())
    @settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
    def test_metric_deviation_roundtrip(self, instance):
        self._assert_roundtrip(instance)

    @given(changes())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_change_roundtrip(self, instance):
        self._assert_roundtrip(instance)

    @given(log_findings_st())
    @settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
    def test_log_findings_roundtrip(self, instance):
        self._assert_roundtrip(instance)

    @given(hypotheses())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_hypothesis_roundtrip(self, instance):
        self._assert_roundtrip(instance)

    @given(blast_radii())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_blast_radius_roundtrip(self, instance):
        self._assert_roundtrip(instance)

    @given(escalation_decisions())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_escalation_decision_roundtrip(self, instance):
        self._assert_roundtrip(instance)

    @given(evidence_items())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_evidence_item_roundtrip(self, instance):
        self._assert_roundtrip(instance)

    @given(skill_responses())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_skill_response_roundtrip(self, instance):
        self._assert_roundtrip(instance)

    @given(skill_errors())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_skill_error_roundtrip(self, instance):
        self._assert_roundtrip(instance)

    @given(slack_messages())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_slack_message_roundtrip(self, instance):
        self._assert_roundtrip(instance)

    @given(conversation_turns())
    @settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
    def test_conversation_turn_roundtrip(self, instance):
        self._assert_roundtrip(instance)

    @given(triage_responses())
    @settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
    def test_triage_response_roundtrip(self, instance):
        self._assert_roundtrip(instance)


# --- Property Tests: Invalid Inputs Produce Structured Errors ---


class TestPropertyInvalidInputs:
    """Property 26 (Part 2): Invalid inputs produce structured ValidationError, never unhandled exceptions.

    **Validates: Requirements 14.3, 14.8, 14.9**
    """

    @given(st.dictionaries(
        keys=safe_text,
        values=st.one_of(
            st.none(),
            st.integers(),
            safe_text,
            st.floats(allow_nan=False, allow_infinity=False),
            st.booleans(),
        ),
        max_size=10,
    ))
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_arbitrary_dict_to_normalized_alert(self, data):
        """Arbitrary dicts either produce a valid NormalizedAlert or a ValidationError."""
        try:
            result = NormalizedAlert.model_validate(data)
            # If it succeeds, it must be a valid NormalizedAlert
            assert isinstance(result, NormalizedAlert)
            assert result.id is not None
            assert result.source in AlertSource
            assert result.severity in Severity
        except ValidationError as e:
            # Structured error — this is the expected behavior for invalid input
            assert len(e.errors()) > 0
            for error in e.errors():
                assert "type" in error
                assert "msg" in error

    @given(st.dictionaries(
        keys=safe_text,
        values=st.one_of(
            st.none(),
            st.integers(),
            safe_text,
            st.floats(allow_nan=False, allow_infinity=False),
            st.booleans(),
        ),
        max_size=10,
    ))
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_arbitrary_dict_to_metric_deviation(self, data):
        """Arbitrary dicts either produce a valid MetricDeviation or a ValidationError."""
        try:
            result = MetricDeviation.model_validate(data)
            assert isinstance(result, MetricDeviation)
            assert 0.0 <= result.confidence <= 1.0
        except ValidationError as e:
            assert len(e.errors()) > 0
            for error in e.errors():
                assert "type" in error
                assert "msg" in error

    @given(st.dictionaries(
        keys=safe_text,
        values=st.one_of(
            st.none(),
            st.integers(),
            safe_text,
            st.floats(allow_nan=False, allow_infinity=False),
            st.booleans(),
        ),
        max_size=10,
    ))
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_arbitrary_dict_to_hypothesis(self, data):
        """Arbitrary dicts either produce a valid Hypothesis or a ValidationError."""
        try:
            result = Hypothesis.model_validate(data)
            assert isinstance(result, Hypothesis)
            assert 0.0 <= result.confidence <= 1.0
        except ValidationError as e:
            assert len(e.errors()) > 0
            for error in e.errors():
                assert "type" in error
                assert "msg" in error

    @given(st.dictionaries(
        keys=safe_text,
        values=st.one_of(
            st.none(),
            st.integers(),
            safe_text,
            st.floats(allow_nan=False, allow_infinity=False),
            st.booleans(),
        ),
        max_size=10,
    ))
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_arbitrary_dict_to_skill_response(self, data):
        """Arbitrary dicts either produce a valid SkillResponse or a ValidationError."""
        try:
            result = SkillResponse.model_validate(data)
            assert isinstance(result, SkillResponse)
            assert result.status in ("success", "error")
        except ValidationError as e:
            assert len(e.errors()) > 0
            for error in e.errors():
                assert "type" in error
                assert "msg" in error

    @given(st.dictionaries(
        keys=safe_text,
        values=st.one_of(
            st.none(),
            st.integers(),
            safe_text,
            st.floats(allow_nan=False, allow_infinity=False),
            st.booleans(),
        ),
        max_size=10,
    ))
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_arbitrary_dict_to_escalation_decision(self, data):
        """Arbitrary dicts either produce a valid EscalationDecision or a ValidationError."""
        try:
            result = EscalationDecision.model_validate(data)
            assert isinstance(result, EscalationDecision)
            assert result.urgency in EscalationUrgency
        except ValidationError as e:
            assert len(e.errors()) > 0
            for error in e.errors():
                assert "type" in error
                assert "msg" in error

    @given(st.dictionaries(
        keys=safe_text,
        values=st.one_of(
            st.none(),
            st.integers(),
            safe_text,
            st.floats(allow_nan=False, allow_infinity=False),
            st.booleans(),
        ),
        max_size=10,
    ))
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_arbitrary_dict_to_change(self, data):
        """Arbitrary dicts either produce a valid Change or a ValidationError."""
        try:
            result = Change.model_validate(data)
            assert isinstance(result, Change)
            assert 0.0 <= result.correlation_score <= 1.0
        except ValidationError as e:
            assert len(e.errors()) > 0
            for error in e.errors():
                assert "type" in error
                assert "msg" in error

    @given(
        confidence=st.floats(allow_nan=False, allow_infinity=False).filter(
            lambda x: x < 0.0 or x > 1.0
        )
    )
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_out_of_range_confidence_always_rejected(self, confidence):
        """Confidence values outside [0, 1] must always produce ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            Hypothesis(
                rank=1,
                title="test",
                description="test",
                confidence=confidence,
            )
        errors = exc_info.value.errors()
        assert len(errors) > 0
        assert any("confidence" in str(e.get("loc", "")) for e in errors)

    @given(
        score=st.floats(allow_nan=False, allow_infinity=False).filter(
            lambda x: x < 0.0 or x > 1.0
        )
    )
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_out_of_range_correlation_score_always_rejected(self, score):
        """Correlation scores outside [0, 1] must always produce ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            Change(
                id="c1",
                type=ChangeType.DEPLOYMENT,
                source=ChangeSource.KUBERNETES,
                timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
                actor="bot",
                description="test",
                affected_resource="deployment/test",
                temporal_proximity=1.0,
                resource_overlap=True,
                correlation_score=score,
            )
        errors = exc_info.value.errors()
        assert len(errors) > 0

    @given(
        weight=st.floats(allow_nan=False, allow_infinity=False).filter(
            lambda x: x < 0.0 or x > 1.0
        )
    )
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_out_of_range_weight_always_rejected(self, weight):
        """Evidence item weight outside [0, 1] must always produce ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            EvidenceItem(
                claim="test",
                source="test",
                weight=weight,
            )
        errors = exc_info.value.errors()
        assert len(errors) > 0

    @given(invalid_source=safe_text.filter(
        lambda s: s not in [e.value for e in AlertSource]
    ))
    @settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
    def test_invalid_alert_source_always_rejected(self, invalid_source):
        """Invalid alert source strings must always produce ValidationError."""
        with pytest.raises(ValidationError):
            NormalizedAlert(
                id="test",
                source=invalid_source,
                severity=Severity.HIGH,
                title="test",
                fired_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                region="us-east-1",
            )

    @given(invalid_status=safe_text.filter(
        lambda s: s not in ("success", "error")
    ))
    @settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
    def test_invalid_skill_response_status_always_rejected(self, invalid_status):
        """SkillResponse status must be 'success' or 'error'; anything else raises ValidationError."""
        with pytest.raises(ValidationError):
            SkillResponse(status=invalid_status)

    @given(invalid_role=safe_text.filter(
        lambda s: s not in ("user", "agent")
    ))
    @settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
    def test_invalid_conversation_turn_role_always_rejected(self, invalid_role):
        """ConversationTurn role must be 'user' or 'agent'; anything else raises ValidationError."""
        with pytest.raises(ValidationError):
            ConversationTurn(
                role=invalid_role,
                content="test",
                timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            )


# --- Property Tests: JSON Output is Always Valid ---


class TestPropertyJsonValidity:
    """Verify serialized output is always valid JSON (Requirement 14.8).

    **Validates: Requirements 14.3, 14.8, 14.9**
    """

    @given(normalized_alerts())
    @settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
    def test_normalized_alert_produces_valid_json(self, instance):
        """Serialized NormalizedAlert is always parseable JSON with 'id' key."""
        json_str = instance.model_dump_json(by_alias=True)
        parsed = json.loads(json_str)
        assert isinstance(parsed, dict)
        assert "id" in parsed
        assert "source" in parsed
        assert "severity" in parsed

    @given(skill_responses())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_skill_response_produces_valid_json_with_status(self, instance):
        """SkillResponse JSON always has 'status' field per Requirement 14.8."""
        json_str = instance.model_dump_json(by_alias=True)
        parsed = json.loads(json_str)
        assert isinstance(parsed, dict)
        assert "status" in parsed
        assert parsed["status"] in ("success", "error")

    @given(skill_errors())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_skill_error_produces_valid_json_with_required_fields(self, instance):
        """SkillError JSON always has skillName, errorType, message fields."""
        json_str = instance.model_dump_json(by_alias=True)
        parsed = json.loads(json_str)
        assert isinstance(parsed, dict)
        assert "skillName" in parsed
        assert "errorType" in parsed
        assert "message" in parsed
