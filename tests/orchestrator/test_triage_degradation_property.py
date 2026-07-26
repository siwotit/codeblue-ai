"""Property tests for triage session graceful degradation.

**Validates: Requirement 13.10**

For any session where skills fail, verify:
1. The session doesn't crash — always returns a valid TriageResponse
2. Previously accumulated evidence is preserved after failures
3. The session can still be used for subsequent interactions after failures

Uses hypothesis to generate arbitrary subsets of skills to fail, then verifies
the session degrades gracefully without data loss.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from orchestrator.interactive_triage import (
    _get_session,
    _skill_registry,
    clear_sessions,
    follow_up,
    invoke_skill,
    register_skill,
    start_investigation,
)
from skills.shared.config import CodeBlueConfig
from skills.shared.models import TriageResponse


# ---------------------------------------------------------------------------
# Skill definitions
# ---------------------------------------------------------------------------

ALL_SKILLS = [
    "metric-baseline",
    "log-triage",
    "k8s-cluster-health",
    "pod-failure-triage",
    "deploy-correlation",
    "hypothesis-engine",
    "node-condition-check",
    "eks-addon-status",
]


def _make_success_skill(name: str):
    """Create a success skill function that returns evidence tagged with its name."""
    def _skill(params: dict[str, Any]) -> dict[str, Any]:
        return {
            "summary": f"{name} completed successfully",
            "evidence_items": [
                {
                    "claim": f"Evidence from {name}",
                    "source": name,
                    "weight": 0.7,
                }
            ],
        }
    return _skill


def _make_failing_skill(name: str):
    """Create a skill function that always raises an exception."""
    def _skill(params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(f"Simulated failure in {name}")
    return _skill


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Generate subsets of skills that will fail (at least 1, up to all)
failing_skills_strategy = st.lists(
    st.sampled_from(ALL_SKILLS),
    min_size=1,
    max_size=len(ALL_SKILLS),
    unique=True,
)

# Generate prompts that trigger different skill invocations
trigger_prompts_strategy = st.sampled_from([
    "check the metrics for anomalies",
    "look at the logs for errors",
    "check cluster health and node conditions",
    "what about pod failures?",
    "check recent deployments and changes",
    "run hypothesis analysis",
    "check the EKS addon status",
    "are there any node conditions?",
])

# Generate arbitrary non-empty prompts
arbitrary_prompt_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
    min_size=3,
    max_size=150,
).filter(lambda s: s.strip() != "")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_state():
    """Ensure clean session store and skill registry for each test."""
    clear_sessions()
    # Start with all skills succeeding
    for skill_name in ALL_SKILLS:
        register_skill(skill_name, _make_success_skill(skill_name))
    yield
    clear_sessions()


def _reset_all_skills() -> None:
    """Re-register all skills as succeeding (reset state between examples)."""
    for skill_name in ALL_SKILLS:
        register_skill(skill_name, _make_success_skill(skill_name))


def _register_failing_subset(failing_names: list[str]) -> None:
    """Register failing versions for a subset of skills."""
    for name in failing_names:
        register_skill(name, _make_failing_skill(name))


def _create_session() -> str:
    """Create a triage session and return its ID."""
    clear_sessions()
    _reset_all_skills()
    config = CodeBlueConfig(region="us-east-1")
    session, _, _ = start_investigation(
        "our prod-cluster is throwing 503 errors in us-east-1",
        config=config,
    )
    return session.session_id


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------


class TestTriageSessionGracefulDegradation:
    """Property 27: Triage session graceful degradation.

    **Validates: Requirement 13.10**

    For any session where skills fail, verify:
    - Session doesn't crash — always returns a valid TriageResponse
    - Previously accumulated evidence is preserved after failures
    - The session can still be used for subsequent interactions after failures
    """

    @given(failing_skills=failing_skills_strategy)
    @settings(
        max_examples=50,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_session_never_crashes_on_skill_failures(
        self, failing_skills: list[str]
    ) -> None:
        """For any subset of failing skills, invoking them never crashes the session."""
        session_id = _create_session()

        # Register failing versions
        _register_failing_subset(failing_skills)

        # Try invoking each failing skill — none should crash the session
        for skill_name in failing_skills:
            response = invoke_skill(session_id, skill_name, {})

            # Must always return a valid TriageResponse
            assert isinstance(response, TriageResponse)
            assert response.summary is not None
            assert isinstance(response.summary, str)
            assert len(response.summary) > 0
            assert response.new_evidence is not None
            assert response.updated_hypotheses is not None
            assert response.suggested_next_steps is not None
            assert len(response.suggested_next_steps) >= 1

    @given(failing_skills=failing_skills_strategy)
    @settings(
        max_examples=50,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_evidence_preserved_after_failures(
        self, failing_skills: list[str]
    ) -> None:
        """Previously accumulated evidence is preserved when skills fail."""
        session_id = _create_session()

        # First: accumulate some evidence with a working skill
        # Pick a skill NOT in the failing set
        working_skills = [s for s in ALL_SKILLS if s not in failing_skills]
        if not working_skills:
            # All skills fail — evidence before should be 0, and should stay 0
            working_skills = []

        # Collect some evidence with working skills before registering failures
        for skill_name in working_skills[:2]:
            invoke_skill(session_id, skill_name, {})

        session = _get_session(session_id)
        evidence_before = len(session.evidence_accumulator)

        # Now register failing versions
        _register_failing_subset(failing_skills)

        # Invoke failing skills
        for skill_name in failing_skills[:3]:
            invoke_skill(session_id, skill_name, {})

        # Evidence should NOT have decreased
        session = _get_session(session_id)
        evidence_after = len(session.evidence_accumulator)
        assert evidence_after >= evidence_before

    @given(
        failing_skills=failing_skills_strategy,
        prompt=trigger_prompts_strategy,
    )
    @settings(
        max_examples=40,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_session_usable_after_failures(
        self, failing_skills: list[str], prompt: str
    ) -> None:
        """Session remains usable for subsequent interactions after skill failures."""
        session_id = _create_session()

        # Register failing versions
        _register_failing_subset(failing_skills)

        # Invoke some failing skills
        for skill_name in failing_skills[:2]:
            invoke_skill(session_id, skill_name, {})

        # Now restore a working skill and verify session still works
        working_skill = "metric-baseline"
        register_skill(working_skill, _make_success_skill(working_skill))

        # Session should still be usable
        response = invoke_skill(session_id, working_skill, {})
        assert isinstance(response, TriageResponse)
        assert response.summary is not None
        assert len(response.summary) > 0

        # follow_up should also still work
        response2 = follow_up(session_id, prompt)
        assert isinstance(response2, TriageResponse)
        assert response2.summary is not None
        assert len(response2.summary) > 0
        assert response2.suggested_next_steps is not None
        assert len(response2.suggested_next_steps) >= 1

    @given(failing_skills=failing_skills_strategy)
    @settings(
        max_examples=30,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_follow_up_with_failing_skills_returns_valid_response(
        self, failing_skills: list[str]
    ) -> None:
        """follow_up still returns valid TriageResponse even when triggered skills fail."""
        session_id = _create_session()

        # Register failing versions
        _register_failing_subset(failing_skills)

        # follow_up may trigger some of these skills based on keyword matching
        # It should still return a valid response regardless
        response = follow_up(session_id, "check metrics and logs and cluster health")

        assert isinstance(response, TriageResponse)
        assert response.summary is not None
        assert isinstance(response.summary, str)
        assert len(response.summary) > 0
        assert response.new_evidence is not None
        assert response.updated_hypotheses is not None
        assert response.suggested_next_steps is not None
        assert len(response.suggested_next_steps) >= 1

    @given(
        failing_skills=st.lists(
            st.sampled_from(ALL_SKILLS),
            min_size=1,
            max_size=3,
            unique=True,
        ),
    )
    @settings(
        max_examples=30,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_remaining_skills_continue_working(
        self, failing_skills: list[str]
    ) -> None:
        """When some skills fail, remaining skills continue providing evidence."""
        session_id = _create_session()

        # Register failing versions only for the selected subset
        _register_failing_subset(failing_skills)

        # Invoke a working skill (one not in failing set)
        working_skills = [s for s in ALL_SKILLS if s not in failing_skills]
        if working_skills:
            response = invoke_skill(session_id, working_skills[0], {})
            assert isinstance(response, TriageResponse)
            # Working skill should produce evidence
            assert len(response.new_evidence) > 0

            # Session evidence should include the working skill's output
            session = _get_session(session_id)
            sources = [e.source for e in session.evidence_accumulator]
            assert working_skills[0] in sources
