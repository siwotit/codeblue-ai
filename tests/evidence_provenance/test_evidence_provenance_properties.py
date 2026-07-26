"""Property-based tests for Evidence Provenance completeness.

**Property 14: Evidence provenance completeness**
**Validates: Requirements 9.1, 9.2, 9.3, 9.4, 9.5, 9.6**

For any claim, verify:
- Source reference with timestamps within query ranges
- Correct link type per source (CloudWatch -> consoleUrl with "cloudwatch",
  Kubernetes -> verificationCommand with "kubectl",
  CloudTrail -> consoleUrl with "cloudtrailv2")
- queryParameters includes timeRange when resolved
- Timestamps within declared query ranges
- Unresolvable claims have non-empty unavailabilityReason
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "evidence-provenance"))

from evidence_provenance import annotate_findings


# ---------------------------------------------------------------------------
# Strategies: generate valid findings for each source type
# ---------------------------------------------------------------------------

# Valid AWS regions
_AWS_REGIONS = st.sampled_from([
    "us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1",
    "eu-central-1", "ap-northeast-1", "us-east-2",
])

# Generate a valid time range (start < end, within reasonable bounds)
_TIME_RANGE = st.builds(
    lambda offset_hours, duration_hours: {
        "start": (datetime(2024, 1, 15, tzinfo=timezone.utc) + timedelta(hours=offset_hours)).isoformat().replace("+00:00", "Z"),
        "end": (datetime(2024, 1, 15, tzinfo=timezone.utc) + timedelta(hours=offset_hours + duration_hours)).isoformat().replace("+00:00", "Z"),
    },
    offset_hours=st.integers(min_value=0, max_value=23),
    duration_hours=st.integers(min_value=1, max_value=4),
)

# Generate a timestamp that falls within a given time range
def _timestamp_within_range(time_range: dict) -> st.SearchStrategy[str]:
    """Generate a timestamp guaranteed to be within the given time range."""
    start = datetime.fromisoformat(time_range["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(time_range["end"].replace("Z", "+00:00"))
    delta_seconds = int((end - start).total_seconds())
    return st.integers(min_value=0, max_value=delta_seconds).map(
        lambda s: (start + timedelta(seconds=s)).isoformat().replace("+00:00", "Z")
    )


# Metric names and namespaces
_METRIC_NAMES = st.sampled_from([
    "CPUUtilization", "MemoryUtilization", "NetworkIn", "NetworkOut",
    "DiskReadOps", "Latency", "ErrorCount", "RequestCount",
])

_METRIC_NAMESPACES = st.sampled_from([
    "AWS/EC2", "AWS/ELB", "AWS/RDS", "AWS/Lambda",
    "Custom/App", "AWS/ECS", "AWS/SQS",
])

# Kubernetes resource kinds
_K8S_RESOURCE_KINDS = st.sampled_from([
    "pod", "deployment", "replicaset", "statefulset",
    "daemonset", "node", "event", "service",
])

_K8S_NAMES = st.from_regex(r"[a-z][a-z0-9\-]{3,20}", fullmatch=True)
_K8S_NAMESPACES = st.sampled_from([
    "default", "production", "staging", "kube-system",
    "monitoring", "payments", "api",
])
_K8S_CLUSTERS = st.sampled_from([
    "eks-prod", "eks-staging", "prod-us-east-1",
    "dev-cluster", "staging-cluster",
])

# CloudTrail event IDs
_EVENT_IDS = st.from_regex(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", fullmatch=True)

# Dimension key-value pairs
_DIMENSIONS = st.dictionaries(
    keys=st.sampled_from(["InstanceId", "LoadBalancerName", "FunctionName", "QueueName"]),
    values=st.from_regex(r"[a-z0-9\-]{5,20}", fullmatch=True),
    min_size=0,
    max_size=3,
)


# ---------------------------------------------------------------------------
# Composite strategies for valid findings
# ---------------------------------------------------------------------------

@st.composite
def cloudwatch_finding(draw):
    """Generate a valid CloudWatch finding with all required fields."""
    time_range = draw(_TIME_RANGE)
    timestamp = draw(_timestamp_within_range(time_range))
    return {
        "claim": f"CloudWatch metric deviation: {draw(_METRIC_NAMES)}",
        "source": "cloudwatch",
        "timestamp": timestamp,
        "sourceDetails": {
            "metricName": draw(_METRIC_NAMES),
            "metricNamespace": draw(_METRIC_NAMESPACES),
            "dimensions": draw(_DIMENSIONS),
            "timeRange": time_range,
        },
    }


@st.composite
def kubernetes_finding(draw):
    """Generate a valid Kubernetes finding with all required fields."""
    return {
        "claim": f"K8s resource issue: {draw(_K8S_NAMES)}",
        "source": "kubernetes",
        "timestamp": datetime(2024, 1, 15, 10, 30, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
        "sourceDetails": {
            "resourceKind": draw(_K8S_RESOURCE_KINDS),
            "resourceName": draw(_K8S_NAMES),
            "namespace": draw(_K8S_NAMESPACES),
            "clusterContext": draw(_K8S_CLUSTERS),
        },
    }


@st.composite
def cloudtrail_finding(draw):
    """Generate a valid CloudTrail finding with all required fields."""
    time_range = draw(_TIME_RANGE)
    timestamp = draw(_timestamp_within_range(time_range))
    return {
        "claim": f"CloudTrail event: API call detected",
        "source": "cloudtrail",
        "timestamp": timestamp,
        "sourceDetails": {
            "eventId": draw(_EVENT_IDS),
            "timeRange": time_range,
        },
    }


@st.composite
def unresolvable_finding(draw):
    """Generate a finding that cannot be resolved (missing required fields or unknown source)."""
    strategy = draw(st.sampled_from(["unknown_source", "missing_fields"]))
    if strategy == "unknown_source":
        return {
            "claim": "Something happened from unknown source",
            "source": draw(st.from_regex(r"unknown_[a-z]{3,8}", fullmatch=True)),
            "timestamp": "2024-01-15T10:30:00Z",
            "sourceDetails": {},
        }
    else:
        # CloudWatch missing required fields
        return {
            "claim": "Metric was high but details missing",
            "source": "cloudwatch",
            "timestamp": "2024-01-15T10:30:00Z",
            "sourceDetails": {
                # Deliberately missing metricName and metricNamespace
                "timeRange": {
                    "start": "2024-01-15T10:00:00Z",
                    "end": "2024-01-15T11:00:00Z",
                },
            },
        }


# Mixed findings list
@st.composite
def findings_list(draw):
    """Generate a mixed list of findings from different source types."""
    cw_findings = draw(st.lists(cloudwatch_finding(), min_size=0, max_size=3))
    k8s_findings = draw(st.lists(kubernetes_finding(), min_size=0, max_size=3))
    ct_findings = draw(st.lists(cloudtrail_finding(), min_size=0, max_size=3))
    all_findings = cw_findings + k8s_findings + ct_findings
    assume(len(all_findings) > 0)
    return all_findings


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------


class TestEvidenceProvenanceCompleteness:
    """Property 14: Evidence provenance completeness.

    Validates: Requirements 9.1, 9.2, 9.3, 9.4, 9.5, 9.6
    """

    @given(finding=cloudwatch_finding(), region=_AWS_REGIONS)
    @settings(max_examples=100)
    def test_cloudwatch_findings_have_console_url_with_cloudwatch(self, finding, region):
        """Req 9.2: CloudWatch findings have a consoleUrl containing 'cloudwatch'.

        For any valid CloudWatch finding with complete source details,
        the annotated result must have provenanceResolved=True and a
        consoleUrl that contains 'cloudwatch'.
        """
        input_data = {"findings": [finding], "region": region}
        result = annotate_findings(input_data)

        assert result["status"] == "success"
        annotated = result["data"]["annotatedFindings"][0]
        assert annotated["provenanceResolved"] is True
        assert annotated["consoleUrl"] is not None
        assert "cloudwatch" in annotated["consoleUrl"]

    @given(finding=kubernetes_finding(), region=_AWS_REGIONS)
    @settings(max_examples=100)
    def test_kubernetes_findings_have_kubectl_command(self, finding, region):
        """Req 9.3: Kubernetes findings have a verificationCommand containing 'kubectl'.

        For any valid Kubernetes finding with complete source details,
        the annotated result must have provenanceResolved=True and a
        verificationCommand that contains 'kubectl'.
        """
        input_data = {"findings": [finding], "region": region}
        result = annotate_findings(input_data)

        assert result["status"] == "success"
        annotated = result["data"]["annotatedFindings"][0]
        assert annotated["provenanceResolved"] is True
        assert annotated["verificationCommand"] is not None
        assert "kubectl" in annotated["verificationCommand"]

    @given(finding=cloudtrail_finding(), region=_AWS_REGIONS)
    @settings(max_examples=100)
    def test_cloudtrail_findings_have_console_url_with_cloudtrailv2(self, finding, region):
        """Req 9.4: CloudTrail findings have a consoleUrl containing 'cloudtrailv2'.

        For any valid CloudTrail finding with complete source details,
        the annotated result must have provenanceResolved=True and a
        consoleUrl that contains 'cloudtrailv2'.
        """
        input_data = {"findings": [finding], "region": region}
        result = annotate_findings(input_data)

        assert result["status"] == "success"
        annotated = result["data"]["annotatedFindings"][0]
        assert annotated["provenanceResolved"] is True
        assert annotated["consoleUrl"] is not None
        assert "cloudtrailv2" in annotated["consoleUrl"]

    @given(findings=findings_list(), region=_AWS_REGIONS)
    @settings(max_examples=80)
    def test_resolved_findings_include_time_range_in_query_parameters(self, findings, region):
        """Req 9.5: queryParameters includes timeRange when resolved.

        For any resolved finding, the queryParameters must contain a
        timeRange field that is not None.
        """
        input_data = {"findings": findings, "region": region}
        result = annotate_findings(input_data)

        assert result["status"] == "success"
        for annotated in result["data"]["annotatedFindings"]:
            if annotated["provenanceResolved"]:
                assert "queryParameters" in annotated
                assert annotated["queryParameters"] is not None
                assert "timeRange" in annotated["queryParameters"]

    @given(finding=cloudwatch_finding(), region=_AWS_REGIONS)
    @settings(max_examples=100)
    def test_cloudwatch_timestamps_within_declared_query_ranges(self, finding, region):
        """Req 9.6: Timestamps within declared query ranges.

        For any CloudWatch finding where the timestamp is within the
        declared time range, provenance must be resolved successfully
        (the implementation rejects timestamps outside the range).
        """
        input_data = {"findings": [finding], "region": region}
        result = annotate_findings(input_data)

        assert result["status"] == "success"
        annotated = result["data"]["annotatedFindings"][0]
        # Since our strategy generates timestamps within the time range,
        # provenance should always be resolved
        assert annotated["provenanceResolved"] is True
        assert annotated["unavailabilityReason"] is None

    @given(finding=cloudtrail_finding(), region=_AWS_REGIONS)
    @settings(max_examples=100)
    def test_cloudtrail_timestamps_within_declared_query_ranges(self, finding, region):
        """Req 9.6: Timestamps within declared query ranges (CloudTrail).

        For any CloudTrail finding where the timestamp is within the
        declared time range, provenance must be resolved successfully.
        """
        input_data = {"findings": [finding], "region": region}
        result = annotate_findings(input_data)

        assert result["status"] == "success"
        annotated = result["data"]["annotatedFindings"][0]
        assert annotated["provenanceResolved"] is True
        assert annotated["unavailabilityReason"] is None

    @given(finding=unresolvable_finding(), region=_AWS_REGIONS)
    @settings(max_examples=50)
    def test_unresolvable_claims_have_non_empty_unavailability_reason(self, finding, region):
        """Req 9.7: Unresolvable claims have non-empty unavailabilityReason.

        For any finding that cannot be resolved (unknown source or
        missing required fields), the annotated result must have
        provenanceResolved=False and a non-empty unavailabilityReason.
        """
        input_data = {"findings": [finding], "region": region}
        result = annotate_findings(input_data)

        assert result["status"] == "success"
        annotated = result["data"]["annotatedFindings"][0]
        assert annotated["provenanceResolved"] is False
        assert annotated["unavailabilityReason"] is not None
        assert len(annotated["unavailabilityReason"]) > 0

    @given(finding=cloudwatch_finding(), region=_AWS_REGIONS)
    @settings(max_examples=80)
    def test_cloudwatch_resolved_has_timestamp_in_output(self, finding, region):
        """Req 9.1: Every resolved claim has a source reference with timestamp.

        For any resolved CloudWatch finding, the annotated result must
        include a non-null timestamp field (the observation time).
        """
        input_data = {"findings": [finding], "region": region}
        result = annotate_findings(input_data)

        assert result["status"] == "success"
        annotated = result["data"]["annotatedFindings"][0]
        assert annotated["provenanceResolved"] is True
        assert annotated["timestamp"] is not None
        assert len(annotated["timestamp"]) > 0

    @given(finding=kubernetes_finding(), region=_AWS_REGIONS)
    @settings(max_examples=80)
    def test_kubernetes_resolved_has_timestamp_in_output(self, finding, region):
        """Req 9.1: Every resolved claim has a source reference with timestamp.

        For any resolved Kubernetes finding, the annotated result must
        include a non-null timestamp field (the observation time).
        """
        input_data = {"findings": [finding], "region": region}
        result = annotate_findings(input_data)

        assert result["status"] == "success"
        annotated = result["data"]["annotatedFindings"][0]
        assert annotated["provenanceResolved"] is True
        assert annotated["timestamp"] is not None
        assert len(annotated["timestamp"]) > 0

    @given(finding=cloudtrail_finding(), region=_AWS_REGIONS)
    @settings(max_examples=80)
    def test_cloudtrail_resolved_has_timestamp_in_output(self, finding, region):
        """Req 9.1: Every resolved claim has a source reference with timestamp.

        For any resolved CloudTrail finding, the annotated result must
        include a non-null timestamp field (the observation time).
        """
        input_data = {"findings": [finding], "region": region}
        result = annotate_findings(input_data)

        assert result["status"] == "success"
        annotated = result["data"]["annotatedFindings"][0]
        assert annotated["provenanceResolved"] is True
        assert annotated["timestamp"] is not None
        assert len(annotated["timestamp"]) > 0
