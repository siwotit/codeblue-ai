"""Property-based test for evidence accumulation monotonicity.

**Property 23: Evidence accumulation monotonicity**
**Validates: Requirements 13.5, 13.6, 13.7**

For any sequence of skill invocations, verify evidenceAccumulator size is
monotonically non-decreasing. Evidence is never removed, even when skills fail.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from orchestrator.interactive_triage import (
    clear_sessions,
    follow_up,
    invoke_skill,
    register_skill,
    start_investigation,
    _get_session,
    _skill_registry,
)
from skills.shared.config import CodeBlueConfig


# ---------------------------------------------------------------------------
# Registered skill names used in the property tests
# ---------------------------------------------------------------------------

SKILL_NAMES = [
    "metric-baseline",
    "log-triage",
    "k8s-cluster-health",
    "deploy-correlation",
    "hypothesis-engine",
]


# ---------------------------------------------------------------------------
# Mock skills that produce evidence
# ---------------------------------------------------------------------------


def _mock_metric_baseline(params: dict[str, Any]) -> dict[str, Any]:
    """Returns metric evidence."""
    return {
        "summary": "CPU at 92%, 3.1σ above baseline",
        "evidence_items": [
            {"claim": "CPU at 92%, 3.1σ above baseline", "source": "metric-baseline", "weight": 0.8},
        ],
    }


def _mock_log_triage(params: dict[str, Any]) -> dict[str, Any]:
    """Returns log evidence."""
    return {
        "summary": "Found 38 ConnectionRefused errors",
        "evidence_items": [
            {"claim": "38 ConnectionRefused errors in last hour", "source": "log-triage", "weight": 0.7},
            {"claim": "Error rate 4x baseline", "source": "log-triage", "weight": 0.6},
        ],
    }


def _mock_k8s_cluster_health(params: dict[str, Any]) -> dict[str, Any]:
    """Returns k8s cluster evidence."""
    return {
        "summary": "2 nodes NotReady, 5 pending pods",
        "evidence_items": [
            {"claim": "2 nodes in NotReady state", "source": "k8s-cluster-health", "weight": 0.9},
        ],
    }


def _mock_deploy_correlation(params: dict[str, Any]) -> dict[str, Any]:
    """Returns deploy correlation evidence."""
    return {
        "summary": "Deployment 12min before incident",
        "evidence_items": [
            {"claim": "payments-service v2.4.0 deployed 12min before incident", "source": "deploy-correlation", "weight": 0.75},
        ],
    }


def _mock_hypothesis_engine(params: dict[str, Any]) -> dict[str, Any]:
    """Returns hypothesis evidence."""
    return {
        "summary": "Top hypothesis: deployment caused failures",
        "evidence_items": [
            {"claim": "Deployment of payments-service likely root cause", "source": "hypothesis-engine", "weight": 0.82},
        ],
    }


def _mock_failing_skill(params: dict[str, Any]) -> dict[str, Any]:
    """A skill that always raises."""
    raise RuntimeError("Simulated skill failure: connection timeout")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_state():
    """Ensure clean session store and skill registry for each test."""
    clear_sessions()
    _skill_registry.clear()
    register_skill("metric-baseline", _mock_metric_baseline)
    register_skill("log-triage", _mock_log_triage)
    register_skill("k8s-cluster-health", _mock_k8s_cluster_health)
    register_skill("deploy-correlation", _mock_deploy_correlation)
    register_skill("hypothesis-engine", _mock_hypothesis_engine)
    yield
    clear_sessions()
    _skill_registry.clear()


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Strategy: generate a random permutation of skill invocations (1 to 10 calls)
skill_sequence_strategy = st.lists(
    st.sampled_from(SKILL_NAMES),
    min_size=1,
    max_size=10,
)

# Strategy: mixed sequence with potential failures (True = use failing skill)
mixed_invocation_strategy = st.lists(
    st.tuples(
        st.sampled_from(SKILL_NAMES),
        st.booleans(),  # True = make it fail
    ),
    min_size=1,
    max_size=8,
)

# Strategy: follow-up prompts that trigger different skills
follow_up_prompts = st.sampled_from([
    "check metrics for anomalies",
    "look at the logs for errors",
    "check cluster health",
    "any recent deployments?",
    "what's your hypothesis?",
    "check pod failures",
    "check node conditions",
])

follow_up_sequence_strategy = st.lists(
    follow_up_prompts,
    min_size=1,
    max_size=6,
)


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------


@settings(max_examples=50, deadline=10000)
@given(skill_sequence=skill_sequence_strategy)
def test_evidence_accumulator_monotonically_nondecreasing(
    skill_sequence: list[str],
) -> None:
    """Property: For any sequence of skill invocations via invoke_skill,
    the evidenceAccumulator size is monotonically non-decreasing.

    **Validates: Requirements 13.5, 13.6, 13.7**
    """
    # Fresh state for each hypothesis example
    clear_sessions()
    _skill_registry.clear()
    register_skill("metric-baseline", _mock_metric_baseline)
    register_skill("log-triage", _mock_log_triage)
    register_skill("k8s-cluster-health", _mock_k8s_cluster_health)
    register_skill("deploy-correlation", _mock_deploy_correlation)
    register_skill("hypothesis-engine", _mock_hypothesis_engine)

    config = CodeBlueConfig(region="us-east-1")
    session, _, _ = start_investigation(
        "prod-cluster throwing 503s in us-east-1", config=config
    )
    session_id = session.session_id

    sizes: list[int] = []
    for skill_name in skill_sequence:
        invoke_skill(session_id, skill_name, {})
        session = _get_session(session_id)
        sizes.append(len(session.evidence_accumulator))

    # Verify monotonically non-decreasing
    for i in range(1, len(sizes)):
        assert sizes[i] >= sizes[i - 1], (
            f"Evidence accumulator decreased from {sizes[i-1]} to {sizes[i]} "
            f"after invoking skill '{skill_sequence[i]}' (step {i}). "
            f"Full size sequence: {sizes}"
        )


@settings(max_examples=50, deadline=10000)
@given(mixed_sequence=mixed_invocation_strategy)
def test_evidence_never_removed_even_on_skill_failure(
    mixed_sequence: list[tuple[str, bool]],
) -> None:
    """Property: Evidence is never removed even when skills fail.
    For any mix of successful and failing skill invocations, the
    evidenceAccumulator size is monotonically non-decreasing.

    **Validates: Requirements 13.5, 13.6, 13.7**
    """
    clear_sessions()
    _skill_registry.clear()
    register_skill("metric-baseline", _mock_metric_baseline)
    register_skill("log-triage", _mock_log_triage)
    register_skill("k8s-cluster-health", _mock_k8s_cluster_health)
    register_skill("deploy-correlation", _mock_deploy_correlation)
    register_skill("hypothesis-engine", _mock_hypothesis_engine)

    # Register a failing variant of each skill
    failing_skill_name = "always-fails"
    register_skill(failing_skill_name, _mock_failing_skill)

    config = CodeBlueConfig(region="us-east-1")
    session, _, _ = start_investigation(
        "prod-cluster throwing 503s in us-east-1", config=config
    )
    session_id = session.session_id

    sizes: list[int] = []
    for skill_name, should_fail in mixed_sequence:
        actual_skill = failing_skill_name if should_fail else skill_name
        invoke_skill(session_id, actual_skill, {})
        session = _get_session(session_id)
        sizes.append(len(session.evidence_accumulator))

    # Verify monotonically non-decreasing
    for i in range(1, len(sizes)):
        assert sizes[i] >= sizes[i - 1], (
            f"Evidence accumulator decreased from {sizes[i-1]} to {sizes[i]} "
            f"at step {i} (skill='{mixed_sequence[i][0]}', "
            f"should_fail={mixed_sequence[i][1]}). "
            f"Full size sequence: {sizes}"
        )


@settings(max_examples=50, deadline=10000)
@given(follow_ups=follow_up_sequence_strategy)
def test_evidence_monotonic_across_follow_up_calls(
    follow_ups: list[str],
) -> None:
    """Property: For any sequence of follow_up calls, the
    evidenceAccumulator size is monotonically non-decreasing.

    **Validates: Requirements 13.5, 13.6, 13.7**
    """
    clear_sessions()
    _skill_registry.clear()
    register_skill("metric-baseline", _mock_metric_baseline)
    register_skill("log-triage", _mock_log_triage)
    register_skill("k8s-cluster-health", _mock_k8s_cluster_health)
    register_skill("deploy-correlation", _mock_deploy_correlation)
    register_skill("hypothesis-engine", _mock_hypothesis_engine)

    config = CodeBlueConfig(region="us-east-1")
    session, _, _ = start_investigation(
        "prod-cluster throwing 503s in us-east-1", config=config
    )
    session_id = session.session_id

    sizes: list[int] = []
    for prompt in follow_ups:
        follow_up(session_id, prompt)
        session = _get_session(session_id)
        sizes.append(len(session.evidence_accumulator))

    # Verify monotonically non-decreasing
    for i in range(1, len(sizes)):
        assert sizes[i] >= sizes[i - 1], (
            f"Evidence accumulator decreased from {sizes[i-1]} to {sizes[i]} "
            f"after follow_up('{follow_ups[i]}') at step {i}. "
            f"Full size sequence: {sizes}"
        )


@settings(max_examples=30, deadline=10000)
@given(
    skill_sequence=skill_sequence_strategy,
    follow_ups=follow_up_sequence_strategy,
)
def test_evidence_monotonic_across_mixed_interactions(
    skill_sequence: list[str],
    follow_ups: list[str],
) -> None:
    """Property: When mixing invoke_skill and follow_up calls,
    the evidenceAccumulator size is still monotonically non-decreasing.

    **Validates: Requirements 13.5, 13.6, 13.7**
    """
    clear_sessions()
    _skill_registry.clear()
    register_skill("metric-baseline", _mock_metric_baseline)
    register_skill("log-triage", _mock_log_triage)
    register_skill("k8s-cluster-health", _mock_k8s_cluster_health)
    register_skill("deploy-correlation", _mock_deploy_correlation)
    register_skill("hypothesis-engine", _mock_hypothesis_engine)

    config = CodeBlueConfig(region="us-east-1")
    session, _, _ = start_investigation(
        "prod-cluster throwing 503s in us-east-1", config=config
    )
    session_id = session.session_id

    sizes: list[int] = []

    # First do some invoke_skill calls
    for skill_name in skill_sequence:
        invoke_skill(session_id, skill_name, {})
        session = _get_session(session_id)
        sizes.append(len(session.evidence_accumulator))

    # Then do some follow_up calls
    for prompt in follow_ups:
        follow_up(session_id, prompt)
        session = _get_session(session_id)
        sizes.append(len(session.evidence_accumulator))

    # Verify monotonically non-decreasing across the entire sequence
    for i in range(1, len(sizes)):
        assert sizes[i] >= sizes[i - 1], (
            f"Evidence accumulator decreased from {sizes[i-1]} to {sizes[i]} "
            f"at step {i}. Full size sequence: {sizes}"
        )
