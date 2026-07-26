"""Tests for interactive triage session management.

Validates:
- Entity extraction from various descriptions
- NormalizedAlert synthesis with defaults
- Session creation with correct initial state
- Field annotation (inferred vs explicit)

Requirements: 13.1, 13.2, 13.3
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from orchestrator.interactive_triage import (
    ExtractedEntities,
    FieldAnnotation,
    _extract_clusters,
    _extract_services,
    _extract_severity,
    extract_entities,
    start_investigation,
    synthesize_alert,
)
from skills.shared.config import CodeBlueConfig
from skills.shared.models import (
    AlertSource,
    AlertState,
    Severity,
)


# ---------------------------------------------------------------------------
# Entity Extraction Tests
# ---------------------------------------------------------------------------


class TestEntityExtraction:
    """Tests for extract_entities function."""

    def test_extracts_aws_region(self) -> None:
        """Regions like us-east-1 are extracted from descriptions."""
        entities = extract_entities("our service in us-east-1 is throwing errors")
        assert "us-east-1" in entities.regions

    def test_extracts_multiple_regions(self) -> None:
        """Multiple regions are extracted."""
        entities = extract_entities(
            "seeing issues in both us-east-1 and eu-west-2"
        )
        assert "us-east-1" in entities.regions
        assert "eu-west-2" in entities.regions

    def test_extracts_http_codes(self) -> None:
        """HTTP status codes (5xx, 4xx) are extracted."""
        entities = extract_entities("getting 503 and 500 errors on the API")
        assert "503" in entities.http_codes
        assert "500" in entities.http_codes

    def test_extracts_cluster_name_explicit(self) -> None:
        """Cluster names in 'cluster X' pattern are extracted."""
        entities = extract_entities("cluster prod-us-east is unhealthy")
        assert "prod-us-east" in entities.clusters

    def test_extracts_cluster_name_with_eks_keyword(self) -> None:
        """Cluster names containing 'eks' are extracted."""
        entities = extract_entities("my-eks-cluster is showing pod failures")
        assert "my-eks-cluster" in entities.clusters

    def test_extracts_cluster_with_prod_prefix(self) -> None:
        """Cluster names with 'prod-' prefix are extracted."""
        entities = extract_entities("our prod-cluster is throwing 503s")
        assert "prod-cluster" in entities.clusters

    def test_extracts_service_names(self) -> None:
        """Service names like 'payment-service' are extracted."""
        entities = extract_entities("payment-service is returning 500s")
        assert "payment-service" in entities.services

    def test_extracts_service_keywords(self) -> None:
        """Known service keywords (api, gateway) are recognized."""
        entities = extract_entities("the api is slow and gateway is timing out")
        assert "api" in entities.services
        assert "gateway" in entities.services

    def test_extracts_namespace(self) -> None:
        """Namespace patterns are extracted."""
        entities = extract_entities(
            "pods in the payments namespace are crashing"
        )
        assert "payments" in entities.namespaces

    def test_extracts_namespace_with_colon_format(self) -> None:
        """Namespace patterns like 'namespace: kube-system' are extracted."""
        entities = extract_entities("namespace: kube-system is having issues")
        assert "kube-system" in entities.namespaces

    def test_extracts_severity_critical(self) -> None:
        """Critical severity keywords are detected."""
        entities = extract_entities("critical outage in production")
        assert entities.severity == Severity.CRITICAL

    def test_extracts_severity_high(self) -> None:
        """High severity keywords are detected."""
        entities = extract_entities("seeing major issues with the service")
        assert entities.severity == Severity.HIGH

    def test_extracts_severity_from_down_keyword(self) -> None:
        """'down' keyword maps to critical severity."""
        entities = extract_entities("the API is down")
        assert entities.severity == Severity.CRITICAL

    def test_no_severity_when_absent(self) -> None:
        """Returns None severity when no indicators are present."""
        entities = extract_entities("seeing some errors in the logs")
        assert entities.severity is None

    def test_preserves_raw_description(self) -> None:
        """Raw description is preserved in extracted entities."""
        desc = "our prod cluster is throwing 503s"
        entities = extract_entities(desc)
        assert entities.raw_description == desc

    def test_complex_description(self) -> None:
        """Complex description with multiple entities is parsed correctly."""
        desc = (
            "critical: prod-cluster in us-west-2 is throwing 503 errors, "
            "payment-service pods in the payments namespace are crashing"
        )
        entities = extract_entities(desc)
        assert entities.severity == Severity.CRITICAL
        assert "us-west-2" in entities.regions
        assert "503" in entities.http_codes
        assert "prod-cluster" in entities.clusters
        assert "payment-service" in entities.services
        assert "payments" in entities.namespaces


# ---------------------------------------------------------------------------
# Alert Synthesis Tests
# ---------------------------------------------------------------------------


class TestAlertSynthesis:
    """Tests for synthesize_alert function."""

    def _make_config(self) -> CodeBlueConfig:
        """Create a test config."""
        return CodeBlueConfig(region="us-east-1")

    def test_synthesizes_alert_with_explicit_region(self) -> None:
        """Region from description is used when available."""
        entities = ExtractedEntities(
            regions=["eu-west-1"],
            raw_description="issues in eu-west-1",
        )
        alert, annotations = synthesize_alert(entities, self._make_config())
        assert alert.region == "eu-west-1"
        region_ann = next(a for a in annotations if a.field_name == "region")
        assert region_ann.source == "explicit"

    def test_synthesizes_alert_with_default_region(self) -> None:
        """Config default region is used when not in description."""
        entities = ExtractedEntities(raw_description="things are broken")
        alert, annotations = synthesize_alert(entities, self._make_config())
        assert alert.region == "us-east-1"
        region_ann = next(a for a in annotations if a.field_name == "region")
        assert region_ann.source == "inferred"

    def test_synthesizes_alert_with_explicit_severity(self) -> None:
        """Severity from description is used when available."""
        entities = ExtractedEntities(
            severity=Severity.CRITICAL,
            raw_description="critical outage",
        )
        alert, annotations = synthesize_alert(entities, self._make_config())
        assert alert.severity == Severity.CRITICAL
        sev_ann = next(a for a in annotations if a.field_name == "severity")
        assert sev_ann.source == "explicit"

    def test_synthesizes_alert_with_default_severity(self) -> None:
        """Severity defaults to medium when not in description."""
        entities = ExtractedEntities(raw_description="some issues")
        alert, annotations = synthesize_alert(entities, self._make_config())
        assert alert.severity == Severity.MEDIUM
        sev_ann = next(a for a in annotations if a.field_name == "severity")
        assert sev_ann.source == "inferred"

    def test_synthesizes_alert_with_cluster(self) -> None:
        """Cluster from description populates alert.cluster."""
        entities = ExtractedEntities(
            clusters=["prod-cluster"],
            raw_description="prod-cluster issues",
        )
        alert, annotations = synthesize_alert(entities, self._make_config())
        assert alert.cluster == "prod-cluster"
        assert alert.source == AlertSource.KUBERNETES

    def test_synthesizes_alert_without_cluster(self) -> None:
        """No cluster means source defaults to CloudWatch."""
        entities = ExtractedEntities(raw_description="CPU high")
        alert, annotations = synthesize_alert(entities, self._make_config())
        assert alert.cluster is None
        assert alert.source == AlertSource.CLOUDWATCH

    def test_synthesizes_alert_with_namespace(self) -> None:
        """Namespace from description populates alert.namespace."""
        entities = ExtractedEntities(
            namespaces=["payments"],
            raw_description="payments namespace",
        )
        alert, annotations = synthesize_alert(entities, self._make_config())
        assert alert.namespace == "payments"

    def test_affected_resources_from_clusters_and_services(self) -> None:
        """Affected resources are built from clusters and services."""
        entities = ExtractedEntities(
            clusters=["prod-cluster"],
            services=["payment-service"],
            raw_description="test",
        )
        alert, _ = synthesize_alert(entities, self._make_config())
        assert len(alert.affected_resources) == 2
        values = [r.value for r in alert.affected_resources]
        assert "prod-cluster" in values
        assert "payment-service" in values

    def test_alert_id_has_triage_prefix(self) -> None:
        """Alert IDs have a 'triage-' prefix for identification."""
        entities = ExtractedEntities(raw_description="test")
        alert, _ = synthesize_alert(entities, self._make_config())
        assert alert.id.startswith("triage-")

    def test_alert_state_is_firing(self) -> None:
        """Synthesized alerts always have state=firing."""
        entities = ExtractedEntities(raw_description="test")
        alert, _ = synthesize_alert(entities, self._make_config())
        assert alert.state == AlertState.FIRING

    def test_raw_payload_contains_metadata(self) -> None:
        """Raw payload contains interactive triage metadata."""
        entities = ExtractedEntities(
            clusters=["my-cluster"],
            raw_description="checking my-cluster",
        )
        alert, _ = synthesize_alert(entities, self._make_config())
        assert alert.raw_payload["interactive_triage"] is True
        assert alert.raw_payload["user_description"] == "checking my-cluster"
        assert "my-cluster" in alert.raw_payload["extracted_entities"]["clusters"]

    def test_title_includes_http_codes(self) -> None:
        """Title includes HTTP codes when present."""
        entities = ExtractedEntities(
            http_codes=["503"],
            raw_description="getting 503 errors",
        )
        alert, _ = synthesize_alert(entities, self._make_config())
        assert "503" in alert.title


# ---------------------------------------------------------------------------
# Session Creation Tests
# ---------------------------------------------------------------------------


class TestSessionCreation:
    """Tests for start_investigation session management."""

    def test_creates_session_with_unique_id(self) -> None:
        """Each session gets a unique ID."""
        config = CodeBlueConfig(region="us-east-1")
        session1, _, _ = start_investigation("issue 1", config=config)
        session2, _, _ = start_investigation("issue 2", config=config)
        assert session1.session_id != session2.session_id

    def test_creates_session_with_empty_evidence_accumulator(self) -> None:
        """New session starts with empty evidence accumulator."""
        config = CodeBlueConfig(region="us-east-1")
        session, _, _ = start_investigation("some issue", config=config)
        assert session.evidence_accumulator == []

    def test_creates_session_with_empty_hypotheses(self) -> None:
        """New session starts with no active hypotheses."""
        config = CodeBlueConfig(region="us-east-1")
        session, _, _ = start_investigation("some issue", config=config)
        assert session.active_hypotheses == []

    def test_creates_session_with_initial_conversation_turn(self) -> None:
        """New session has one conversation turn (the user prompt)."""
        config = CodeBlueConfig(region="us-east-1")
        prompt = "our API is down"
        session, _, _ = start_investigation(prompt, config=config)
        assert len(session.conversation_history) == 1
        turn = session.conversation_history[0]
        assert turn.role == "user"
        assert turn.content == prompt

    def test_creates_session_with_normalized_alert(self) -> None:
        """Session contains a synthesized NormalizedAlert."""
        config = CodeBlueConfig(region="us-west-2")
        session, _, _ = start_investigation(
            "cluster prod-cluster is unhealthy", config=config
        )
        assert session.normalized_alert is not None
        assert session.normalized_alert.cluster == "prod-cluster"

    def test_initial_skills_include_metric_baseline(self) -> None:
        """Initial skills always include metric-baseline."""
        config = CodeBlueConfig(region="us-east-1")
        _, _, skills = start_investigation("some issue", config=config)
        assert "metric-baseline" in skills

    def test_initial_skills_include_log_triage(self) -> None:
        """Initial skills always include log-triage."""
        config = CodeBlueConfig(region="us-east-1")
        _, _, skills = start_investigation("some issue", config=config)
        assert "log-triage" in skills

    def test_initial_skills_include_k8s_when_cluster_present(self) -> None:
        """K8s skills are included when a cluster is identified."""
        config = CodeBlueConfig(region="us-east-1")
        _, _, skills = start_investigation(
            "prod-cluster is failing", config=config
        )
        assert "k8s-cluster-health" in skills

    def test_initial_skills_exclude_k8s_when_no_cluster(self) -> None:
        """K8s skills are excluded when no cluster is identified."""
        config = CodeBlueConfig(region="us-east-1")
        _, _, skills = start_investigation(
            "high CPU on the API", config=config
        )
        assert "k8s-cluster-health" not in skills

    def test_at_least_one_skill_invoked(self) -> None:
        """At least one signal-collection skill is always invoked."""
        config = CodeBlueConfig(region="us-east-1")
        _, _, skills = start_investigation("something is wrong", config=config)
        assert len(skills) >= 1


# ---------------------------------------------------------------------------
# Field Annotation Tests
# ---------------------------------------------------------------------------


class TestFieldAnnotations:
    """Tests for field annotation (inferred vs explicit)."""

    def test_explicit_fields_marked_explicit(self) -> None:
        """Fields extracted from description are marked as explicit."""
        config = CodeBlueConfig(region="us-east-1")
        _, annotations, _ = start_investigation(
            "critical issue in us-west-2", config=config
        )
        severity_ann = next(
            a for a in annotations if a.field_name == "severity"
        )
        region_ann = next(a for a in annotations if a.field_name == "region")
        assert severity_ann.source == "explicit"
        assert region_ann.source == "explicit"

    def test_inferred_fields_marked_inferred(self) -> None:
        """Fields using defaults are marked as inferred."""
        config = CodeBlueConfig(region="us-east-1")
        _, annotations, _ = start_investigation(
            "something is wrong with the API", config=config
        )
        severity_ann = next(
            a for a in annotations if a.field_name == "severity"
        )
        region_ann = next(a for a in annotations if a.field_name == "region")
        assert severity_ann.source == "inferred"
        assert region_ann.source == "inferred"

    def test_annotations_contain_reasons(self) -> None:
        """All annotations include a reason string."""
        config = CodeBlueConfig(region="us-east-1")
        _, annotations, _ = start_investigation("test issue", config=config)
        for ann in annotations:
            assert ann.reason != "", f"Annotation for {ann.field_name} has no reason"

    def test_annotation_to_dict(self) -> None:
        """FieldAnnotation serializes to dict correctly."""
        ann = FieldAnnotation(
            field_name="severity",
            value="medium",
            source="inferred",
            reason="No severity found",
        )
        d = ann.to_dict()
        assert d["field_name"] == "severity"
        assert d["value"] == "medium"
        assert d["source"] == "inferred"
        assert d["reason"] == "No severity found"
