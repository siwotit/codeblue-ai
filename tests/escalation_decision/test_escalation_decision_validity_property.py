"""Property-based test: Escalation decision validity.

**Validates: Requirements 8.3, 8.4**

For any incident assessment, verify:
1. urgency is one of: page_now, notify_channel, business_hours (Req 8.3)
2. reason is non-empty (Req 8.4)
3. escalate is a boolean
4. escalate is True when urgency is page_now or notify_channel, False when business_hours
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[2] / "skills" / "escalation-decision"),
)

from escalation_decision import make_escalation_decision


# ---------------------------------------------------------------------------
# Valid urgency levels per Requirement 8.3
# ---------------------------------------------------------------------------

VALID_URGENCY_LEVELS = {"page_now", "notify_channel", "business_hours"}

# ---------------------------------------------------------------------------
# Strategies for generating valid EscalationInput dicts
# ---------------------------------------------------------------------------

severity_strategy = st.sampled_from(["critical", "high", "medium", "low"])

service_name_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=1,
    max_size=30,
)

affected_services_strategy = st.lists(
    service_name_strategy,
    min_size=0,
    max_size=10,
)

blast_radius_strategy = st.one_of(
    st.none(),
    st.fixed_dictionaries({
        "affectedServices": affected_services_strategy,
    }),
    st.fixed_dictionaries({
        "affectedServices": affected_services_strategy,
        "impactedPodCount": st.integers(min_value=0, max_value=500),
        "region": st.sampled_from(["us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1"]),
    }),
)

# Generate valid UTC timestamps (ISO 8601 format)
hour_strategy = st.integers(min_value=0, max_value=23)
time_strategy = st.builds(
    lambda h: f"2024-01-15T{h:02d}:00:00Z",
    h=hour_strategy,
)

timezone_strategy = st.sampled_from([
    "UTC",
    "America/New_York",
    "America/Los_Angeles",
    "Europe/London",
    "Asia/Tokyo",
    "Australia/Sydney",
])

service_criticality_strategy = st.one_of(
    st.none(),
    st.dictionaries(
        keys=service_name_strategy,
        values=st.sampled_from(["tier-1", "tier-2", "tier-3"]),
        min_size=0,
        max_size=5,
    ),
)

unavailable_sources_strategy = st.one_of(
    st.none(),
    st.lists(
        st.sampled_from([
            "metricDeviation",
            "logTriage",
            "deployCorrelation",
            "clusterHealth",
        ]),
        min_size=0,
        max_size=4,
    ),
)


@st.composite
def escalation_input_strategy(draw):
    """Generate a valid EscalationInput dict with all required and optional fields."""
    severity = draw(severity_strategy)
    blast_radius = draw(blast_radius_strategy)
    current_time_utc = draw(time_strategy)
    team_timezone = draw(timezone_strategy)
    service_criticality = draw(service_criticality_strategy)
    unavailable_sources = draw(unavailable_sources_strategy)

    input_data = {"severity": severity}

    if blast_radius is not None:
        input_data["blastRadius"] = blast_radius

    input_data["currentTimeUtc"] = current_time_utc
    input_data["teamTimezone"] = team_timezone

    if service_criticality is not None:
        input_data["serviceCriticality"] = service_criticality

    if unavailable_sources is not None:
        input_data["unavailableSources"] = unavailable_sources

    return input_data


# ---------------------------------------------------------------------------
# Property 16: Escalation decision validity
# ---------------------------------------------------------------------------


class TestEscalationDecisionValidityProperty:
    """Property 16: Escalation decision validity.

    **Validates: Requirements 8.3, 8.4**
    """

    @given(input_data=escalation_input_strategy())
    @settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
    def test_urgency_is_valid_level(self, input_data: dict) -> None:
        """For any valid input, urgency must be one of the three valid levels (Req 8.3)."""
        result = make_escalation_decision(input_data)

        assert result["status"] == "success", (
            f"Expected success but got error: {result.get('message')}"
        )
        urgency = result["data"]["urgency"]
        assert urgency in VALID_URGENCY_LEVELS, (
            f"Urgency '{urgency}' is not in valid set {VALID_URGENCY_LEVELS}. "
            f"Input: severity={input_data.get('severity')}"
        )

    @given(input_data=escalation_input_strategy())
    @settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
    def test_reason_is_non_empty(self, input_data: dict) -> None:
        """For any valid input, reason must be a non-empty string (Req 8.4)."""
        result = make_escalation_decision(input_data)

        assert result["status"] == "success", (
            f"Expected success but got error: {result.get('message')}"
        )
        reason = result["data"]["reason"]
        assert isinstance(reason, str), f"Reason should be a string, got {type(reason)}"
        assert len(reason) > 0, "Reason must be non-empty"

    @given(input_data=escalation_input_strategy())
    @settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
    def test_escalate_is_boolean(self, input_data: dict) -> None:
        """For any valid input, escalate must be a boolean."""
        result = make_escalation_decision(input_data)

        assert result["status"] == "success", (
            f"Expected success but got error: {result.get('message')}"
        )
        escalate = result["data"]["escalate"]
        assert isinstance(escalate, bool), (
            f"escalate should be a bool, got {type(escalate)}: {escalate}"
        )

    @given(input_data=escalation_input_strategy())
    @settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
    def test_escalate_consistent_with_urgency(self, input_data: dict) -> None:
        """Escalate is True when urgency is page_now or notify_channel, False for business_hours."""
        result = make_escalation_decision(input_data)

        assert result["status"] == "success", (
            f"Expected success but got error: {result.get('message')}"
        )
        urgency = result["data"]["urgency"]
        escalate = result["data"]["escalate"]

        if urgency in ("page_now", "notify_channel"):
            assert escalate is True, (
                f"escalate should be True when urgency is '{urgency}', "
                f"but got False. Input: {input_data}"
            )
        elif urgency == "business_hours":
            assert escalate is False, (
                f"escalate should be False when urgency is 'business_hours', "
                f"but got True. Input: {input_data}"
            )
