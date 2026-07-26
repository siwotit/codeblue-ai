"""Unit tests for the Evidence Provenance skill."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "skills" / "evidence-provenance"))

from evidence_provenance import (
    annotate_findings,
    _generate_cloudwatch_url,
    _generate_cloudtrail_url,
    _generate_kubectl_command,
    _is_timestamp_within_range,
    _parse_iso_timestamp,
)


# ---------------------------------------------------------------------------
# Test: CloudWatch URL generation (Req 9.2)
# ---------------------------------------------------------------------------


class TestCloudWatchUrl:
    """Tests for CloudWatch console deep link generation."""

    def test_basic_cloudwatch_url(self):
        """Generate a CW URL with metric name, namespace, dimensions, region, and time range."""
        source_details = {
            "metricName": "CPUUtilization",
            "metricNamespace": "AWS/EC2",
            "dimensions": {"InstanceId": "i-1234567890abcdef0"},
            "region": "us-east-1",
            "timeRange": {
                "start": "2024-01-15T10:00:00Z",
                "end": "2024-01-15T11:00:00Z",
            },
        }
        url = _generate_cloudwatch_url(source_details, "us-east-1")

        assert "us-east-1.console.aws.amazon.com/cloudwatch/home" in url
        assert "region=us-east-1" in url
        assert "CPUUtilization" in url
        assert "AWS/EC2" in url
        assert "InstanceId" in url
        assert "i-1234567890abcdef0" in url
        assert "2024-01-15T10:00:00Z" in url
        assert "2024-01-15T11:00:00Z" in url

    def test_cloudwatch_url_multiple_dimensions(self):
        """CloudWatch URL includes multiple dimension key-value pairs."""
        source_details = {
            "metricName": "Latency",
            "metricNamespace": "AWS/ELB",
            "dimensions": {
                "LoadBalancerName": "my-lb",
                "AvailabilityZone": "us-west-2a",
            },
            "timeRange": {"start": "2024-01-15T10:00:00Z", "end": "2024-01-15T11:00:00Z"},
        }
        url = _generate_cloudwatch_url(source_details, "us-west-2")

        assert "LoadBalancerName" in url
        assert "my-lb" in url
        assert "AvailabilityZone" in url
        assert "us-west-2a" in url

    def test_cloudwatch_url_no_dimensions(self):
        """CloudWatch URL works without dimensions."""
        source_details = {
            "metricName": "ErrorCount",
            "metricNamespace": "Custom/App",
            "dimensions": {},
            "timeRange": {"start": "2024-01-15T10:00:00Z", "end": "2024-01-15T11:00:00Z"},
        }
        url = _generate_cloudwatch_url(source_details, "eu-west-1")

        assert "ErrorCount" in url
        assert "Custom/App" in url
        assert "eu-west-1" in url


# ---------------------------------------------------------------------------
# Test: CloudTrail URL generation (Req 9.4)
# ---------------------------------------------------------------------------


class TestCloudTrailUrl:
    """Tests for CloudTrail console detail page link generation."""

    def test_basic_cloudtrail_url(self):
        """Generate a CloudTrail event detail URL with event ID and region."""
        source_details = {
            "eventId": "abc123-def456-ghi789",
            "region": "us-east-1",
        }
        url = _generate_cloudtrail_url(source_details, "us-east-1")

        assert "us-east-1.console.aws.amazon.com/cloudtrailv2/home" in url
        assert "region=us-east-1" in url
        assert "#/events/abc123-def456-ghi789" in url

    def test_cloudtrail_url_different_region(self):
        """CloudTrail URL uses the correct region."""
        source_details = {"eventId": "event-xyz"}
        url = _generate_cloudtrail_url(source_details, "ap-southeast-1")

        assert "ap-southeast-1.console.aws.amazon.com/cloudtrailv2/home" in url
        assert "region=ap-southeast-1" in url
        assert "#/events/event-xyz" in url


# ---------------------------------------------------------------------------
# Test: kubectl command generation (Req 9.3)
# ---------------------------------------------------------------------------


class TestKubectlCommand:
    """Tests for kubectl verification command generation."""

    def test_pod_uses_describe(self):
        """Pod resources should use kubectl describe."""
        source_details = {
            "resourceKind": "pod",
            "resourceName": "my-app-abc123",
            "namespace": "production",
            "clusterContext": "eks-cluster-prod",
        }
        cmd = _generate_kubectl_command(source_details)

        assert cmd == "kubectl describe pod my-app-abc123 -n production --context eks-cluster-prod"

    def test_deployment_uses_get_yaml(self):
        """Deployment resources should use kubectl get -o yaml."""
        source_details = {
            "resourceKind": "deployment",
            "resourceName": "web-api",
            "namespace": "default",
            "clusterContext": "staging-cluster",
        }
        cmd = _generate_kubectl_command(source_details)

        assert cmd == "kubectl get deployment web-api -n default --context staging-cluster -o yaml"

    def test_event_uses_get_events_sorted(self):
        """Events should use kubectl get events with time-based sorting."""
        source_details = {
            "resourceKind": "event",
            "resourceName": "",
            "namespace": "kube-system",
            "clusterContext": "prod-cluster",
        }
        cmd = _generate_kubectl_command(source_details)

        assert "kubectl get events" in cmd
        assert "-n kube-system" in cmd
        assert "--context prod-cluster" in cmd
        assert "--sort-by='.lastTimestamp'" in cmd

    def test_replicaset_uses_get_yaml(self):
        """ReplicaSet resources should use kubectl get -o yaml."""
        source_details = {
            "resourceKind": "replicaset",
            "resourceName": "web-api-5d4f7b",
            "namespace": "default",
            "clusterContext": "cluster-1",
        }
        cmd = _generate_kubectl_command(source_details)

        assert "kubectl get replicaset web-api-5d4f7b" in cmd
        assert "-o yaml" in cmd

    def test_node_uses_describe(self):
        """Node resources should use kubectl describe."""
        source_details = {
            "resourceKind": "node",
            "resourceName": "ip-10-0-1-42",
            "namespace": "default",
            "clusterContext": "prod-cluster",
        }
        cmd = _generate_kubectl_command(source_details)

        assert "kubectl describe node ip-10-0-1-42" in cmd


# ---------------------------------------------------------------------------
# Test: Timestamp validation (Req 9.5, 9.6)
# ---------------------------------------------------------------------------


class TestTimestampValidation:
    """Tests for timestamp validation within query time ranges."""

    def test_timestamp_within_range(self):
        """A timestamp within the range should pass validation."""
        is_valid, reason = _is_timestamp_within_range(
            "2024-01-15T10:30:00Z",
            {"start": "2024-01-15T10:00:00Z", "end": "2024-01-15T11:00:00Z"},
        )
        assert is_valid is True
        assert reason is None

    def test_timestamp_outside_range(self):
        """A timestamp outside the range should fail validation."""
        is_valid, reason = _is_timestamp_within_range(
            "2024-01-15T09:00:00Z",
            {"start": "2024-01-15T10:00:00Z", "end": "2024-01-15T11:00:00Z"},
        )
        assert is_valid is False
        assert "falls outside declared query time range" in reason

    def test_timestamp_at_range_boundary(self):
        """A timestamp at the range boundary should pass."""
        is_valid, reason = _is_timestamp_within_range(
            "2024-01-15T10:00:00Z",
            {"start": "2024-01-15T10:00:00Z", "end": "2024-01-15T11:00:00Z"},
        )
        assert is_valid is True

    def test_missing_timestamp_passes(self):
        """Missing timestamp should not block (cannot validate)."""
        is_valid, reason = _is_timestamp_within_range(
            None,
            {"start": "2024-01-15T10:00:00Z", "end": "2024-01-15T11:00:00Z"},
        )
        assert is_valid is True

    def test_missing_time_range_passes(self):
        """Missing time range should not block."""
        is_valid, reason = _is_timestamp_within_range(
            "2024-01-15T10:30:00Z",
            None,
        )
        assert is_valid is True


# ---------------------------------------------------------------------------
# Test: Full annotation flow (Req 9.1, 9.7)
# ---------------------------------------------------------------------------


class TestAnnotateFindings:
    """Tests for the full annotate_findings function."""

    def test_successful_cloudwatch_annotation(self):
        """A valid CloudWatch finding should produce resolved provenance."""
        input_data = {
            "findings": [
                {
                    "claim": "CPU utilization exceeded 95%",
                    "source": "cloudwatch",
                    "timestamp": "2024-01-15T10:30:00Z",
                    "sourceDetails": {
                        "metricName": "CPUUtilization",
                        "metricNamespace": "AWS/EC2",
                        "dimensions": {"InstanceId": "i-abc123"},
                        "timeRange": {
                            "start": "2024-01-15T10:00:00Z",
                            "end": "2024-01-15T11:00:00Z",
                        },
                    },
                }
            ],
            "region": "us-east-1",
        }

        result = annotate_findings(input_data)

        assert result["status"] == "success"
        findings = result["data"]["annotatedFindings"]
        assert len(findings) == 1
        finding = findings[0]
        assert finding["provenanceResolved"] is True
        assert finding["consoleUrl"] is not None
        assert "cloudwatch" in finding["consoleUrl"]
        assert finding["unavailabilityReason"] is None
        assert finding["queryParameters"]["timeRange"] is not None
        assert finding["queryParameters"]["filters"]["metricName"] == "CPUUtilization"

    def test_successful_kubernetes_annotation(self):
        """A valid Kubernetes finding should produce a kubectl command."""
        input_data = {
            "findings": [
                {
                    "claim": "Pod in CrashLoopBackOff",
                    "source": "kubernetes",
                    "timestamp": "2024-01-15T10:30:00Z",
                    "sourceDetails": {
                        "resourceKind": "pod",
                        "resourceName": "api-server-xyz",
                        "namespace": "production",
                        "clusterContext": "eks-prod",
                    },
                }
            ],
            "region": "us-east-1",
        }

        result = annotate_findings(input_data)

        assert result["status"] == "success"
        finding = result["data"]["annotatedFindings"][0]
        assert finding["provenanceResolved"] is True
        assert finding["verificationCommand"] is not None
        assert "kubectl describe pod api-server-xyz" in finding["verificationCommand"]
        assert "-n production" in finding["verificationCommand"]
        assert "--context eks-prod" in finding["verificationCommand"]

    def test_successful_cloudtrail_annotation(self):
        """A valid CloudTrail finding should produce a console URL."""
        input_data = {
            "findings": [
                {
                    "claim": "IAM role modified 5 minutes before incident",
                    "source": "cloudtrail",
                    "timestamp": "2024-01-15T10:25:00Z",
                    "sourceDetails": {
                        "eventId": "event-12345",
                        "timeRange": {
                            "start": "2024-01-15T10:00:00Z",
                            "end": "2024-01-15T11:00:00Z",
                        },
                    },
                }
            ],
            "region": "us-west-2",
        }

        result = annotate_findings(input_data)

        assert result["status"] == "success"
        finding = result["data"]["annotatedFindings"][0]
        assert finding["provenanceResolved"] is True
        assert finding["consoleUrl"] is not None
        assert "cloudtrailv2" in finding["consoleUrl"]
        assert "event-12345" in finding["consoleUrl"]
        assert "us-west-2" in finding["consoleUrl"]

    def test_unrecognized_source_annotated(self):
        """An unrecognized source should produce an unavailability annotation (Req 9.7)."""
        input_data = {
            "findings": [
                {
                    "claim": "Something happened",
                    "source": "unknown_system",
                    "timestamp": "2024-01-15T10:30:00Z",
                    "sourceDetails": {},
                }
            ],
            "region": "us-east-1",
        }

        result = annotate_findings(input_data)

        assert result["status"] == "success"
        finding = result["data"]["annotatedFindings"][0]
        assert finding["provenanceResolved"] is False
        assert "Unrecognized source system: unknown_system" in finding["unavailabilityReason"]

    def test_missing_required_fields_annotated(self):
        """Missing required source details should produce an unavailability annotation."""
        input_data = {
            "findings": [
                {
                    "claim": "Metric was high",
                    "source": "cloudwatch",
                    "timestamp": "2024-01-15T10:30:00Z",
                    "sourceDetails": {
                        # Missing metricName and metricNamespace
                        "timeRange": {
                            "start": "2024-01-15T10:00:00Z",
                            "end": "2024-01-15T11:00:00Z",
                        },
                    },
                }
            ],
            "region": "us-east-1",
        }

        result = annotate_findings(input_data)

        assert result["status"] == "success"
        finding = result["data"]["annotatedFindings"][0]
        assert finding["provenanceResolved"] is False
        assert "missing metricName, metricNamespace" in finding["unavailabilityReason"]

    def test_timestamp_outside_range_flagged(self):
        """A finding with timestamp outside declared range should be flagged (Req 9.6)."""
        input_data = {
            "findings": [
                {
                    "claim": "Old observation",
                    "source": "cloudwatch",
                    "timestamp": "2024-01-14T08:00:00Z",  # Way before the range
                    "sourceDetails": {
                        "metricName": "CPUUtilization",
                        "metricNamespace": "AWS/EC2",
                        "timeRange": {
                            "start": "2024-01-15T10:00:00Z",
                            "end": "2024-01-15T11:00:00Z",
                        },
                    },
                }
            ],
            "region": "us-east-1",
        }

        result = annotate_findings(input_data)

        assert result["status"] == "success"
        finding = result["data"]["annotatedFindings"][0]
        assert finding["provenanceResolved"] is False
        assert "falls outside declared query time range" in finding["unavailabilityReason"]

    def test_missing_region_returns_error(self):
        """Missing region should return an error response."""
        input_data = {"findings": [{"claim": "test", "source": "cloudwatch"}]}

        result = annotate_findings(input_data)

        assert result["status"] == "error"
        assert "region" in result["message"]

    def test_missing_findings_returns_error(self):
        """Missing findings array should return an error response."""
        input_data = {"region": "us-east-1"}

        result = annotate_findings(input_data)

        assert result["status"] == "error"
        assert "findings" in result["message"]

    def test_summary_counts(self):
        """Summary should correctly count resolved and unresolved findings."""
        input_data = {
            "findings": [
                {
                    "claim": "Valid CW finding",
                    "source": "cloudwatch",
                    "timestamp": "2024-01-15T10:30:00Z",
                    "sourceDetails": {
                        "metricName": "CPUUtilization",
                        "metricNamespace": "AWS/EC2",
                        "timeRange": {
                            "start": "2024-01-15T10:00:00Z",
                            "end": "2024-01-15T11:00:00Z",
                        },
                    },
                },
                {
                    "claim": "Missing details",
                    "source": "cloudwatch",
                    "sourceDetails": {},
                },
            ],
            "region": "us-east-1",
        }

        result = annotate_findings(input_data)

        assert result["status"] == "success"
        summary = result["data"]["summary"]
        assert summary["totalFindings"] == 2
        assert summary["resolvedCount"] == 1
        assert summary["unresolvedCount"] == 1

    def test_no_time_range_for_time_sensitive_source(self):
        """CloudWatch without timeRange should be flagged as missing time range."""
        input_data = {
            "findings": [
                {
                    "claim": "Metric spike",
                    "source": "cloudwatch",
                    "timestamp": "2024-01-15T10:30:00Z",
                    "sourceDetails": {
                        "metricName": "Errors",
                        "metricNamespace": "Custom/App",
                        # No timeRange
                    },
                }
            ],
            "region": "us-east-1",
        }

        result = annotate_findings(input_data)

        assert result["status"] == "success"
        finding = result["data"]["annotatedFindings"][0]
        assert finding["provenanceResolved"] is False
        assert "No query time range declared" in finding["unavailabilityReason"]

    def test_individual_failure_does_not_block_batch(self):
        """One failing finding should not prevent others from being processed."""
        input_data = {
            "findings": [
                {
                    "claim": "Bad finding",
                    "source": "unknown_source",
                    "sourceDetails": {},
                },
                {
                    "claim": "Good K8s finding",
                    "source": "kubernetes",
                    "sourceDetails": {
                        "resourceKind": "pod",
                        "resourceName": "test-pod",
                        "namespace": "default",
                        "clusterContext": "test-cluster",
                    },
                },
            ],
            "region": "us-east-1",
        }

        result = annotate_findings(input_data)

        assert result["status"] == "success"
        findings = result["data"]["annotatedFindings"]
        assert len(findings) == 2
        assert findings[0]["provenanceResolved"] is False
        assert findings[1]["provenanceResolved"] is True

    def test_empty_findings_array(self):
        """An empty findings array should succeed with zero counts."""
        input_data = {"findings": [], "region": "us-east-1"}

        result = annotate_findings(input_data)

        assert result["status"] == "success"
        assert result["data"]["summary"]["totalFindings"] == 0
        assert result["data"]["summary"]["resolvedCount"] == 0
        assert result["data"]["summary"]["unresolvedCount"] == 0
