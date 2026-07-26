"""Property tests for TriageResponse structural completeness.

**Validates: Requirements 13.8**

For any valid triage interaction, verify the response has:
- Non-empty summary
- new_evidence list (may be empty but never None)
- updated_hypotheses list (never None)
- suggested_next_steps list with at least 1 item (never None)

Uses hypothesis to generate arbitrary prompts and skill names, then calls
follow_up or invoke_skill and verifies structural completeness of responses.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from orchestrator.interactive_triage import (
    clear_sessions,
    follow_up,
    invoke_skill,
    register_skill,
    start_investigation,
)
from skills.shared.config import CodeBlueConfig
from skills.shared.models import TriageResponse


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Generate arbitrary non-empty prompts for follow-up interactions
prompt_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
    min_size=3,
    max_size=200,
).filter(lambda s: s.strip() != "")

# Skill names that may or may not exist in the registry
skill_name_strategy = st.sampled_from([
    "metric-baseline",
    "log-triage",
    "k8s-cluster-health",
    "pod-failure-triage",
    "deploy-correlation",
    "hypothesis-engine",
    "node-condition-check",
    "eks-addon-status",
    "nonexistent-skill",
    "failing-skill",
])


# ---------------------------------------------------------------------------
# Mock skills for testing
# ---------------------------------------------------------------------------


def _mock_success_skill(params: dict[str, Any]) -> dict[str, Any]:
    """A mock skill that always succeeds with evidence."""
    return {
        "summary": "Skill completed successfully",
        "evidence_items": [
            {
                "claim": "Found relevant signal",
                "source": "test-skill",
                "weight": 0.7,
            }
        ],
    }


def _mock_failing_skill(params: dict[str, Any]) -> dict[str, Any]:
    """A mock skill that always raises an exception."""
    raise RuntimeError("Simulated skill failure")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_state():
    """Ensure clean session store and skill registry for each test."""
    clear_sessions()
    # Register skills: some succeed, some fail
    register_skill("metric-baseline", _mock_success_skill)
    register_skill("log-triage", _mock_success_skill)
    register_skill("k8s-cluster-health", _mock_success_skill)
    register_skill("pod-failure-triage", _mock_success_skill)
    register_skill("deploy-correlation", _mock_success_skill)
    register_skill("hypothesis-engine", _mock_success_skill)
    register_skill("node-condition-check", _mock_success_skill)
    register_skill("eks-addon-status", _mock_success_skill)
    register_skill("failing-skill", _mock_failing_skill)
    yield
    clear_sessions()


def _create_session() -> str:
    """Create a triage session and return its ID."""
    clear_sessions()
    # Re-register all skills fresh for each hypothesis example
    register_skill("metric-baseline", _mock_success_skill)
    register_skill("log-triage", _mock_success_skill)
    register_skill("k8s-cluster-health", _mock_success_skill)
    register_skill("pod-failure-triage", _mock_success_skill)
    register_skill("deploy-correlation", _mock_success_skill)
    register_skill("hypothesis-engine", _mock_success_skill)
    register_skill("node-condition-check", _mock_success_skill)
    register_skill("eks-addon-status", _mock_success_skill)
    register_skill("failing-skill", _mock_failing_skill)

    config = CodeBlueConfig(region="us-east-1")
    session, _, _ = start_investigation(
        "our prod-cluster is throwing 503 errors in us-east-1",
        config=config,
    )
    return session.session_id


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------


class TestTriageResponseStructuralCompleteness:
    """Property 25: TriageResponse structural completeness.

    **Validates: Requirement 13.8**

    For any valid triage interaction, verify response has:
    - non-empty summary
    - new_evidence list (may be empty)
    - updated_hypotheses list
    - suggested_next_steps list with >=1 item
    - None of these fields are None
    """

    @given(prompt=prompt_strategy)
    @settings(
        max_examples=50,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_follow_up_response_always_complete(self, prompt: str) -> None:
        """For any arbitrary follow-up prompt, the response is structurally complete."""
        session_id = _create_session()
        response = follow_up(session_id, prompt)

        # Verify type
        assert isinstance(response, TriageResponse)

        # summary: non-empty string, never None
        assert response.summary is not None
        assert isinstance(response.summary, str)
        assert len(response.summary) > 0

        # new_evidence: list, never None (may be empty)
        assert response.new_evidence is not None
        assert isinstance(response.new_evidence, list)

        # updated_hypotheses: list, never None (may be empty)
        assert response.updated_hypotheses is not None
        assert isinstance(response.updated_hypotheses, list)

        # suggested_next_steps: list with at least 1 item, never None
        assert response.suggested_next_steps is not None
        assert isinstance(response.suggested_next_steps, list)
        assert len(response.suggested_next_steps) >= 1

    @given(skill_name=skill_name_strategy)
    @settings(
        max_examples=50,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_invoke_skill_response_always_complete(self, skill_name: str) -> None:
        """For any skill invocation (success or failure), the response is structurally complete."""
        session_id = _create_session()
        response = invoke_skill(session_id, skill_name, {})

        # Verify type
        assert isinstance(response, TriageResponse)

        # summary: non-empty string, never None
        assert response.summary is not None
        assert isinstance(response.summary, str)
        assert len(response.summary) > 0

        # new_evidence: list, never None (may be empty)
        assert response.new_evidence is not None
        assert isinstance(response.new_evidence, list)

        # updated_hypotheses: list, never None (may be empty)
        assert response.updated_hypotheses is not None
        assert isinstance(response.updated_hypotheses, list)

        # suggested_next_steps: list with at least 1 item, never None
        assert response.suggested_next_steps is not None
        assert isinstance(response.suggested_next_steps, list)
        assert len(response.suggested_next_steps) >= 1

    @given(
        prompt=prompt_strategy,
        skill_name=skill_name_strategy,
    )
    @settings(
        max_examples=30,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_mixed_interactions_always_produce_complete_responses(
        self, prompt: str, skill_name: str
    ) -> None:
        """A follow-up then invoke_skill both return structurally complete responses."""
        session_id = _create_session()

        # First: follow_up
        resp1 = follow_up(session_id, prompt)
        assert resp1.summary is not None and resp1.summary != ""
        assert resp1.new_evidence is not None
        assert resp1.updated_hypotheses is not None
        assert resp1.suggested_next_steps is not None
        assert len(resp1.suggested_next_steps) >= 1

        # Second: invoke_skill
        resp2 = invoke_skill(session_id, skill_name, {})
        assert resp2.summary is not None and resp2.summary != ""
        assert resp2.new_evidence is not None
        assert resp2.updated_hypotheses is not None
        assert resp2.suggested_next_steps is not None
        assert len(resp2.suggested_next_steps) >= 1
