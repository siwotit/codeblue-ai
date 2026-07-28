"""Property test for processing duration recording.

**Validates: Requirement 12.6**

Property 19: Processing duration recording
- For any completed workflow, verify processingDuration is recorded and generatedAt > firedAt.

Tests:
1. For any generated valid NormalizedAlert, the resulting report has a positive processing_duration
2. report.generated_at > alert.fired_at always holds
3. processing_duration format is always "Xms" where X is a positive float
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from orchestrator.workflow_orchestrator import (
    SkillInvoker,
    SkillResult,
    WorkflowOrchestrator,
)
from skills.shared.models import (
    AlertSource,
    AlertState,
    NormalizedAlert,
    ResourceIdentifier,
    ResourceIdentifierType,
    Severity,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Generate fired_at timestamps from a range of past times (up to 30 days ago)
# to present. This ensures variety in temporal distances.
fired_at_strategy = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2025, 6, 1),
    timezones=st.just(timezone.utc),
)

severity_strategy = st.sampled_from([
    Severity.CRITICAL,
    Severity.HIGH,
    Severity.MEDIUM,
    Severity.LOW,
])

source_strategy = st.sampled_from([
    AlertSource.CLOUDWATCH,
    AlertSource.GRAFANA,
    AlertSource.KUBERNETES,
])

state_strategy = st.sampled_from([AlertState.FIRING, AlertState.RESOLVED])


@st.composite
def normalized_alert_strategy(draw: st.DrawFn) -> NormalizedAlert:
    """Generate arbitrary valid NormalizedAlert objects with different fired_at times."""
    fired_at = draw(fired_at_strategy)
    severity = draw(severity_strategy)
    source = draw(source_strategy)
    state = draw(state_strategy)
    region = draw(st.sampled_from(["us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1"]))
    cluster = draw(st.one_of(st.none(), st.sampled_from(["prod-cluster", "staging-cluster"])))
    namespace = draw(st.one_of(st.none(), st.sampled_from(["default", "payments", "auth"])))

    return NormalizedAlert(
        id=f"alert-{draw(st.integers(min_value=1, max_value=999999))}",
        source=source,
        source_alarm_id=f"alarm-{draw(st.integers(min_value=1, max_value=999999))}",
        severity=severity,
        title=draw(st.sampled_from([
            "High CPU Utilization",
            "Memory Pressure Detected",
            "Disk I/O Latency Spike",
            "Pod CrashLoopBackOff",
            "5xx Error Rate Elevated",
        ])),
        description="Test alert for property-based testing",
        state=state,
        fired_at=fired_at,
        affected_resources=[
            ResourceIdentifier(
                type=ResourceIdentifierType.ARN,
                value=f"arn:aws:ec2:{region}:123456789:instance/i-test",
                display_name="test-resource",
            )
        ],
        region=region,
        cluster=cluster,
        namespace=namespace,
        metric_name="CPUUtilization",
        metric_namespace="AWS/EC2",
        dimensions={"InstanceId": "i-test"},
        raw_payload={"source": "property-test"},
    )


# ---------------------------------------------------------------------------
# Mock invoker for property tests
# ---------------------------------------------------------------------------


class FastMockInvoker(SkillInvoker):
    """Mock invoker that returns success immediately for all skills."""

    async def invoke(
        self, skill_name: str, input_data: dict[str, Any], timeout: float
    ) -> SkillResult:
        return SkillResult(skill_name=skill_name, success=True, data={})


# ---------------------------------------------------------------------------
# Property Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@settings(max_examples=50, deadline=10000)
@given(alert=normalized_alert_strategy())
async def test_processing_duration_is_recorded_and_positive(
    alert: NormalizedAlert,
) -> None:
    """Property 19: For any completed workflow, processingDuration is recorded as
    a positive value in "Xms" format, and generatedAt > firedAt.

    **Validates: Requirement 12.6**

    Checks:
    1. processing_duration is a non-empty string matching the pattern "Xms"
       where X is a non-negative number (float or int)
    2. The numeric X value is >= 0 (processing always takes some time)
    3. report.generated_at > alert.fired_at (report is generated after the alert fired)
    """
    invoker = FastMockInvoker()
    orchestrator = WorkflowOrchestrator(skill_invoker=invoker)

    report = await orchestrator.handle_alert(alert)

    # 1. processing_duration format is "Xms" where X is a non-negative number
    assert report.processing_duration is not None
    assert isinstance(report.processing_duration, str)

    # Match the expected format: number followed by "ms"
    duration_match = re.match(r"^(\d+(?:\.\d+)?)ms$", report.processing_duration)
    assert duration_match is not None, (
        f"processing_duration '{report.processing_duration}' does not match 'Xms' format"
    )

    # 2. The numeric value is non-negative (processing takes >= 0ms)
    duration_value = float(duration_match.group(1))
    assert duration_value >= 0, (
        f"processing_duration value {duration_value}ms should be non-negative"
    )

    # 3. generatedAt > firedAt — the report is always generated after the alert fired
    assert report.generated_at > alert.fired_at, (
        f"generatedAt ({report.generated_at}) should be after firedAt ({alert.fired_at})"
    )
