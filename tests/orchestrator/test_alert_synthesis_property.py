"""Property-based test for NormalizedAlert synthesis from natural language.

**Property 21: NormalizedAlert synthesis from natural language**
**Validates: Requirements 13.1, 13.2**

For any description with identifiable entities, verify:
1. NormalizedAlert has all required fields (id, source, severity, title, description, state, fired_at, region)
2. Annotations exist for at least severity and region
3. When entities are present, they appear in the alert (region matches, cluster matches)
4. Alert ID always has "triage-" prefix
"""

from __future__ import annotations

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from orchestrator.interactive_triage import (
    extract_entities,
    synthesize_alert,
    FieldAnnotation,
)
from skills.shared.config import CodeBlueConfig
from skills.shared.models import (
    AlertSource,
    AlertState,
    NormalizedAlert,
    Severity,
)


# ---------------------------------------------------------------------------
# Hypothesis strategies for generating descriptions with entities
# ---------------------------------------------------------------------------

# AWS region components
REGION_PREFIXES = ["us", "eu", "ap", "sa", "ca", "me", "af"]
REGION_DIRECTIONS = [
    "east", "west", "north", "south", "central",
    "northeast", "southeast", "northwest", "southwest",
]
REGION_NUMBERS = ["1", "2", "3"]

# HTTP status codes (4xx and 5xx)
HTTP_CODES = [str(c) for c in range(400, 520)]

# Service names that match extraction patterns
SERVICE_NAMES = [
    "payment-service", "auth-service", "order-service",
    "checkout-api", "user-worker", "cache-service",
    "notification-svc", "shipping-api", "search-worker",
]

# Cluster names that match extraction patterns
CLUSTER_NAMES = [
    "prod-cluster", "staging-cluster", "my-eks-cluster",
    "prod-us-east-1", "staging-eu-west-1", "dev-main",
]

# Severity keywords
SEVERITY_WORDS = [
    "critical", "down", "outage", "high", "severe", "major",
    "medium", "moderate", "elevated", "low", "minor", "degraded",
]

# Filler description phrases
DESCRIPTION_PHRASES = [
    "is throwing errors",
    "has high latency",
    "pods are failing",
    "returning timeouts",
    "experiencing issues",
    "is not responding",
    "showing degraded performance",
    "connections refused",
    "is throwing exceptions",
    "CPU utilization is spiking",
]


def _build_region_string(prefix: str, direction: str, number: str) -> str:
    """Construct a valid AWS region string."""
    return f"{prefix}-{direction}-{number}"


# Strategy: generate a valid AWS region
aws_region_strategy = st.builds(
    _build_region_string,
    st.sampled_from(REGION_PREFIXES),
    st.sampled_from(REGION_DIRECTIONS),
    st.sampled_from(REGION_NUMBERS),
)

# Strategy: generate a description containing a region
description_with_region = st.builds(
    lambda region, phrase: f"cluster in {region} {phrase}",
    aws_region_strategy,
    st.sampled_from(DESCRIPTION_PHRASES),
)

# Strategy: generate a description containing a cluster name
description_with_cluster = st.builds(
    lambda cluster, phrase: f"cluster {cluster} {phrase}",
    st.sampled_from(CLUSTER_NAMES),
    st.sampled_from(DESCRIPTION_PHRASES),
)

# Strategy: generate a description containing an HTTP code
description_with_http_code = st.builds(
    lambda code, phrase: f"getting {code} errors, service {phrase}",
    st.sampled_from(HTTP_CODES),
    st.sampled_from(DESCRIPTION_PHRASES),
)

# Strategy: generate a description containing a service name
description_with_service = st.builds(
    lambda service, phrase: f"the {service} {phrase}",
    st.sampled_from(SERVICE_NAMES),
    st.sampled_from(DESCRIPTION_PHRASES),
)

# Strategy: generate a description with severity keyword
description_with_severity = st.builds(
    lambda severity, phrase: f"{severity} issue, service {phrase}",
    st.sampled_from(SEVERITY_WORDS),
    st.sampled_from(DESCRIPTION_PHRASES),
)

# Composite strategy: combine multiple entity types in a single description
description_with_multiple_entities = st.builds(
    lambda region, cluster, service, severity, phrase: (
        f"{severity}: {service} on cluster {cluster} in {region} {phrase}"
    ),
    aws_region_strategy,
    st.sampled_from(CLUSTER_NAMES),
    st.sampled_from(SERVICE_NAMES),
    st.sampled_from(SEVERITY_WORDS),
    st.sampled_from(DESCRIPTION_PHRASES),
)

# Strategy: any description with at least one entity
description_with_entities = st.one_of(
    description_with_region,
    description_with_cluster,
    description_with_http_code,
    description_with_service,
    description_with_severity,
    description_with_multiple_entities,
)


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=5000)
@given(description=description_with_entities)
def test_synthesized_alert_has_required_fields(description: str) -> None:
    """Property: For any description with identifiable entities, synthesize_alert
    always produces a NormalizedAlert with all required fields populated.

    **Validates: Requirements 13.1, 13.2**
    """
    config = CodeBlueConfig(region="us-east-1")
    entities = extract_entities(description)
    alert, annotations = synthesize_alert(entities, config)

    # Required fields must be present and non-empty
    assert alert.id, "Alert ID must be non-empty"
    assert alert.source is not None, "Alert source must be set"
    assert alert.severity is not None, "Alert severity must be set"
    assert alert.title, "Alert title must be non-empty"
    assert alert.description, "Alert description must be non-empty"
    assert alert.state is not None, "Alert state must be set"
    assert alert.fired_at is not None, "Alert fired_at must be set"
    assert alert.region, "Alert region must be non-empty"

    # Validate types
    assert isinstance(alert.source, AlertSource)
    assert isinstance(alert.severity, Severity)
    assert isinstance(alert.state, AlertState)


@settings(max_examples=100, deadline=5000)
@given(description=description_with_entities)
def test_annotations_include_severity_and_region(description: str) -> None:
    """Property: For any synthesized alert, annotations always include at least
    severity and region entries.

    **Validates: Requirements 13.1, 13.2**
    """
    config = CodeBlueConfig(region="us-east-1")
    entities = extract_entities(description)
    alert, annotations = synthesize_alert(entities, config)

    # Extract annotated field names
    annotated_fields = {ann.field_name for ann in annotations}

    assert "severity" in annotated_fields, (
        f"Severity annotation missing. Annotated fields: {annotated_fields}"
    )
    assert "region" in annotated_fields, (
        f"Region annotation missing. Annotated fields: {annotated_fields}"
    )

    # Each annotation must have a valid source
    for ann in annotations:
        assert ann.source in ("explicit", "inferred"), (
            f"Annotation source must be 'explicit' or 'inferred', got '{ann.source}'"
        )


@settings(max_examples=100, deadline=5000)
@given(description=description_with_region)
def test_region_entity_appears_in_alert(description: str) -> None:
    """Property: When a region is present in the description, it appears as
    the alert's region field with an 'explicit' annotation.

    **Validates: Requirements 13.1, 13.2**
    """
    config = CodeBlueConfig(region="us-west-2")
    entities = extract_entities(description)

    # Only test when region was actually extracted
    assume(len(entities.regions) > 0)

    alert, annotations = synthesize_alert(entities, config)

    # The alert region should match one of the extracted regions
    assert alert.region in entities.regions, (
        f"Alert region '{alert.region}' not in extracted regions {entities.regions}"
    )

    # The region annotation should be marked as explicit
    region_annotations = [a for a in annotations if a.field_name == "region"]
    assert len(region_annotations) == 1
    assert region_annotations[0].source == "explicit"


@settings(max_examples=100, deadline=5000)
@given(description=description_with_cluster)
def test_cluster_entity_appears_in_alert(description: str) -> None:
    """Property: When a cluster name is present in the description, it appears
    in the alert's cluster field.

    **Validates: Requirements 13.1, 13.2**
    """
    config = CodeBlueConfig(region="us-east-1")
    entities = extract_entities(description)

    # Only test when cluster was actually extracted
    assume(len(entities.clusters) > 0)

    alert, annotations = synthesize_alert(entities, config)

    # The alert cluster should match one of the extracted clusters
    assert alert.cluster is not None, "Cluster should be set when entities contain clusters"
    assert alert.cluster in entities.clusters, (
        f"Alert cluster '{alert.cluster}' not in extracted clusters {entities.clusters}"
    )


@settings(max_examples=100, deadline=5000)
@given(description=description_with_entities)
def test_alert_id_has_triage_prefix(description: str) -> None:
    """Property: The synthesized alert ID always has a 'triage-' prefix.

    **Validates: Requirements 13.1, 13.2**
    """
    config = CodeBlueConfig(region="us-east-1")
    entities = extract_entities(description)
    alert, annotations = synthesize_alert(entities, config)

    assert alert.id.startswith("triage-"), (
        f"Alert ID '{alert.id}' does not start with 'triage-' prefix"
    )


@settings(max_examples=50, deadline=5000)
@given(description=description_with_severity)
def test_severity_keyword_produces_non_default_severity(description: str) -> None:
    """Property: When severity keywords are present in the description,
    the alert severity is set based on the keyword (not always defaulting to medium).

    **Validates: Requirements 13.1, 13.2**
    """
    config = CodeBlueConfig(region="us-east-1")
    entities = extract_entities(description)

    # If severity was extracted, it should be explicit in annotations
    assume(entities.severity is not None)

    alert, annotations = synthesize_alert(entities, config)

    # Severity annotation should be marked as explicit
    severity_annotations = [a for a in annotations if a.field_name == "severity"]
    assert len(severity_annotations) == 1
    assert severity_annotations[0].source == "explicit"
    assert alert.severity == entities.severity


@settings(max_examples=100, deadline=5000)
@given(description=description_with_multiple_entities)
def test_multiple_entities_all_reflected_in_alert(description: str) -> None:
    """Property: When multiple entity types are present, synthesize_alert
    reflects all identifiable entities in the resulting alert.

    **Validates: Requirements 13.1, 13.2**
    """
    config = CodeBlueConfig(region="us-west-2")
    entities = extract_entities(description)
    alert, annotations = synthesize_alert(entities, config)

    # The alert should reflect extracted entities
    if entities.regions:
        assert alert.region in entities.regions

    if entities.clusters:
        assert alert.cluster is not None
        assert alert.cluster in entities.clusters

    # Severity should always be set
    assert alert.severity is not None

    # Alert should always have a valid state
    assert alert.state == AlertState.FIRING
