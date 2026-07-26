"""Tests for interactive triage follow-up and evidence accumulation.

Covers:
- follow_up appends conversation turn
- Evidence accumulates monotonically
- Scope narrowing updates the context
- invoke_skill adds evidence
- Skill failure doesn't crash session
- TriageResponse has all required fields

Requirements: 13.4, 13.5, 13.6, 13.7, 13.8, 13.9, 13.10
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from orchestrator.interactive_triage import (
    clear_sessions,
    follow_up,
    invoke_skill,
    register_skill,
    start_investigation,
    _get_session,
)
from skills.shared.models import EvidenceItem, TriageResponse


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_state():
    """Ensure clean session store and skill registry for each test."""
    clear_sessions()
    # Register some test skills
    register_skill("metric-baseline", _mock_metric_skill)
    register_skill("log-triage", _mock_log_skill)
    register_skill("k8s-cluster-health", _mock_k8s_skill)
    register_skill("pod-failure-triage", _mock_pod_skill)
    register_skill("deploy-correlation", _mock_deploy_skill)
    register_skill("hypothesis-engine", _mock_hypothesis_skill)
    register_skill("failing-skill", _mock_failing_skill)
    yield
    clear_sessions()


def _mock_metric_skill(params: dict[str, Any]) -> dict[str, Any]:
    """Mock metric-baseline skill that returns evidence."""
    return {
        "summary": "CPU utilization at 95%, 3.2 stddev above baseline",
        "evidence_items": [
            {
                "claim": "CPU utilization at 95%, 3.2 stddev above baseline",
                "source": "metric-baseline",
                "weight": 0.8,
            }
        ],
    }


def _mock_log_skill(params: dict[str, Any]) -> dict[str, Any]:
    """Mock log-triage skill that returns evidence."""
    return {
        "summary": "Found 42 new ConnectionRefused errors in last hour",
        "evidence_items": [
            {
                "claim": "42 new ConnectionRefused errors in payments-service logs",
                "source": "log-triage",
                "weight": 0.7,
            },
            {
                "claim": "Error rate increased 5x from baseline",
                "source": "log-triage",
                "weight": 0.6,
            },
        ],
    }


def _mock_k8s_skill(params: dict[str, Any]) -> dict[str, Any]:
    """Mock k8s-cluster-health skill."""
    return {
        "summary": "Cluster degraded: 2 nodes not ready",
        "evidence_items": [
            {
                "claim": "2 nodes in NotReady state",
                "source": "k8s-cluster-health",
                "weight": 0.9,
            }
        ],
    }


def _mock_pod_skill(params: dict[str, Any]) -> dict[str, Any]:
    """Mock pod-failure-triage skill."""
    return {
        "summary": "3 pods in CrashLoopBackOff in payments namespace",
        "evidence_items": [
            {
                "claim": "3 pods CrashLoopBackOff in payments namespace",
                "source": "pod-failure-triage",
                "weight": 0.85,
            }
        ],
    }


def _mock_deploy_skill(params: dict[str, Any]) -> dict[str, Any]:
    """Mock deploy-correlation skill."""
    return {
        "summary": "Found deployment 15 minutes before incident",
        "evidence_items": [
            {
                "claim": "payments-service deployed v2.3.1 at 14:32 UTC",
                "source": "deploy-correlation",
                "weight": 0.75,
            }
        ],
    }


def _mock_hypothesis_skill(params: dict[str, Any]) -> dict[str, Any]:
    """Mock hypothesis-engine skill."""
    return {
        "summary": "Top hypothesis: recent deployment caused connection failures",
        "evidence_items": [
            {
                "claim": "Deployment of payments-service v2.3.1 likely caused connection failures",
                "source": "hypothesis-engine",
                "weight": 0.82,
            }
        ],
    }


def _mock_failing_skill(params: dict[str, Any]) -> dict[str, Any]:
    """Mock skill that always raises an exception."""
    raise RuntimeError("Connection to metrics API timed out after 15s")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _create_session(prompt: str = "our prod-cluster is throwing 503s in us-east-1") -> str:
    """Create a triage session and return its ID."""
    session, _annotations, _skills = start_investigation(prompt)
    return session.session_id


# ---------------------------------------------------------------------------
# Tests: follow_up appends conversation turn (Req 13.4)
# ---------------------------------------------------------------------------


class TestFollowUpAppendsTurn:
    """Verify follow_up appends a ConversationTurn with full prior context."""

    def test_follow_up_adds_user_and_agent_turns(self):
        """Follow-up should add both a user turn and an agent turn."""
        session_id = _create_session()
        session_before = _get_session(session_id)
        turns_before = len(session_before.conversation_history)

        follow_up(session_id, "check the logs for timeout errors")

        session_after = _get_session(session_id)
        # Should have added 2 turns: user + agent
        assert len(session_after.conversation_history) == turns_before + 2

    def test_follow_up_user_turn_has_correct_role(self):
        """The user turn should have role='user' and contain the prompt."""
        session_id = _create_session()
        prompt = "what about the pod failures?"
        follow_up(session_id, prompt)

        session = _get_session(session_id)
        # The second-to-last turn (before agent response) should be user
        user_turn = session.conversation_history[-2]
        assert user_turn.role == "user"
        assert user_turn.content == prompt

    def test_follow_up_agent_turn_has_skills_invoked(self):
        """The agent turn should record which skills were invoked."""
        session_id = _create_session()
        follow_up(session_id, "check the logs")

        session = _get_session(session_id)
        agent_turn = session.conversation_history[-1]
        assert agent_turn.role == "agent"
        assert len(agent_turn.skills_invoked) > 0

    def test_follow_up_preserves_prior_turns(self):
        """All prior conversation turns should be preserved."""
        session_id = _create_session()
        session = _get_session(session_id)
        initial_turn = session.conversation_history[0]

        follow_up(session_id, "first follow-up")
        follow_up(session_id, "second follow-up")

        session = _get_session(session_id)
        # Original turn should still be there
        assert session.conversation_history[0].content == initial_turn.content
        assert len(session.conversation_history) == 5  # 1 initial + 2*2 follow-ups

    def test_follow_up_raises_on_invalid_session(self):
        """Should raise KeyError for unknown session ID."""
        with pytest.raises(KeyError):
            follow_up("nonexistent-session-id", "hello")


# ---------------------------------------------------------------------------
# Tests: Evidence accumulates monotonically (Req 13.6, 13.7)
# ---------------------------------------------------------------------------


class TestEvidenceAccumulation:
    """Verify evidence only accumulates and is never removed."""

    def test_evidence_grows_after_follow_up(self):
        """Evidence accumulator size should grow after each follow-up."""
        session_id = _create_session()

        follow_up(session_id, "check metrics for anomalies")
        session = _get_session(session_id)
        count_after_first = len(session.evidence_accumulator)
        assert count_after_first > 0

        follow_up(session_id, "also check the logs")
        session = _get_session(session_id)
        count_after_second = len(session.evidence_accumulator)
        assert count_after_second >= count_after_first

    def test_evidence_never_decreases(self):
        """Evidence count should be monotonically non-decreasing."""
        session_id = _create_session()
        counts: list[int] = []

        prompts = [
            "check metrics",
            "check logs for errors",
            "look at pod failures",
            "check recent deployments",
        ]

        for prompt in prompts:
            follow_up(session_id, prompt)
            session = _get_session(session_id)
            counts.append(len(session.evidence_accumulator))

        # Verify monotonically non-decreasing
        for i in range(1, len(counts)):
            assert counts[i] >= counts[i - 1], (
                f"Evidence decreased from {counts[i-1]} to {counts[i]} at step {i}"
            )

    def test_skill_failure_does_not_reduce_evidence(self):
        """A failing skill should not remove previously collected evidence."""
        session_id = _create_session()

        # First, collect some evidence
        follow_up(session_id, "check metrics")
        session = _get_session(session_id)
        count_before = len(session.evidence_accumulator)
        assert count_before > 0

        # Now invoke a failing skill
        invoke_skill(session_id, "failing-skill", {})
        session = _get_session(session_id)
        count_after = len(session.evidence_accumulator)

        # Evidence should not have decreased
        assert count_after >= count_before


# ---------------------------------------------------------------------------
# Tests: Scope narrowing (Req 13.9)
# ---------------------------------------------------------------------------


class TestScopeNarrowing:
    """Verify scope narrowing updates the session context."""

    def test_namespace_narrowing(self):
        """Follow-up mentioning a namespace should narrow the scope."""
        session_id = _create_session("our prod-cluster has issues")
        session = _get_session(session_id)
        assert session.normalized_alert.namespace is None or session.normalized_alert.namespace == ""

        follow_up(session_id, "dig into the payments namespace")

        session = _get_session(session_id)
        assert session.normalized_alert.namespace == "payments"

    def test_cluster_narrowing(self):
        """Follow-up mentioning a different cluster should update scope."""
        session_id = _create_session("we have issues in us-east-1")

        follow_up(session_id, "check cluster staging-cluster")

        session = _get_session(session_id)
        assert session.normalized_alert.cluster == "staging-cluster"

    def test_scope_narrowing_preserves_evidence(self):
        """Narrowing scope should not remove previously collected evidence."""
        session_id = _create_session("prod-cluster throwing errors")

        follow_up(session_id, "check logs")
        session = _get_session(session_id)
        evidence_before = len(session.evidence_accumulator)

        follow_up(session_id, "focus on the payments namespace")
        session = _get_session(session_id)
        evidence_after = len(session.evidence_accumulator)

        assert evidence_after >= evidence_before


# ---------------------------------------------------------------------------
# Tests: invoke_skill adds evidence (Req 13.5)
# ---------------------------------------------------------------------------


class TestInvokeSkill:
    """Verify direct skill invocation works correctly."""

    def test_invoke_skill_adds_evidence(self):
        """invoke_skill should add evidence to the session."""
        session_id = _create_session()
        session = _get_session(session_id)
        evidence_before = len(session.evidence_accumulator)

        invoke_skill(session_id, "metric-baseline", {"metric": "CPUUtilization"})

        session = _get_session(session_id)
        assert len(session.evidence_accumulator) > evidence_before

    def test_invoke_skill_appends_conversation_turns(self):
        """invoke_skill should append user and agent turns."""
        session_id = _create_session()
        session = _get_session(session_id)
        turns_before = len(session.conversation_history)

        invoke_skill(session_id, "log-triage", {})

        session = _get_session(session_id)
        assert len(session.conversation_history) == turns_before + 2

    def test_invoke_skill_returns_triage_response(self):
        """invoke_skill should return a valid TriageResponse."""
        session_id = _create_session()
        response = invoke_skill(session_id, "k8s-cluster-health", {"cluster": "prod"})

        assert isinstance(response, TriageResponse)
        assert response.summary != ""
        assert len(response.new_evidence) > 0

    def test_invoke_skill_evidence_accumulates_across_calls(self):
        """Multiple invoke_skill calls should accumulate evidence."""
        session_id = _create_session()

        invoke_skill(session_id, "metric-baseline", {})
        session = _get_session(session_id)
        count1 = len(session.evidence_accumulator)

        invoke_skill(session_id, "log-triage", {})
        session = _get_session(session_id)
        count2 = len(session.evidence_accumulator)

        invoke_skill(session_id, "k8s-cluster-health", {})
        session = _get_session(session_id)
        count3 = len(session.evidence_accumulator)

        assert count1 > 0
        assert count2 > count1
        assert count3 > count2

    def test_invoke_skill_raises_on_invalid_session(self):
        """Should raise KeyError for unknown session ID."""
        with pytest.raises(KeyError):
            invoke_skill("nonexistent", "metric-baseline", {})


# ---------------------------------------------------------------------------
# Tests: Skill failure doesn't crash session (Req 13.10)
# ---------------------------------------------------------------------------


class TestSkillFailureGraceful:
    """Verify skill failures are handled gracefully."""

    def test_failing_skill_returns_response(self):
        """A failing skill should still return a TriageResponse."""
        session_id = _create_session()
        response = invoke_skill(session_id, "failing-skill", {})

        assert isinstance(response, TriageResponse)
        assert "failed" in response.summary.lower()

    def test_failing_skill_continues_session(self):
        """Session should remain usable after a skill failure."""
        session_id = _create_session()

        # Invoke a failing skill
        invoke_skill(session_id, "failing-skill", {})

        # Session should still work for subsequent operations
        response = invoke_skill(session_id, "metric-baseline", {})
        assert len(response.new_evidence) > 0

    def test_failing_skill_does_not_crash_follow_up(self):
        """follow_up should handle skill failures within its execution."""
        session_id = _create_session()

        # Register a skill that will be triggered by the prompt but will fail
        register_skill("node-condition-check", _mock_failing_skill)

        # This should not raise even if node-condition-check fails
        response = follow_up(session_id, "check node conditions")
        assert isinstance(response, TriageResponse)

    def test_unavailable_skill_reports_error(self):
        """Invoking an unregistered skill should report the error gracefully."""
        session_id = _create_session()
        response = invoke_skill(session_id, "nonexistent-skill", {})

        assert isinstance(response, TriageResponse)
        assert "not available" in response.summary.lower() or "failed" in response.summary.lower()


# ---------------------------------------------------------------------------
# Tests: TriageResponse has all required fields (Req 13.8)
# ---------------------------------------------------------------------------


class TestTriageResponseCompleteness:
    """Verify every TriageResponse has summary, evidence, hypotheses, next steps."""

    def test_follow_up_response_has_all_fields(self):
        """follow_up response should have summary, evidence, hypotheses, next_steps."""
        session_id = _create_session()
        response = follow_up(session_id, "check the metrics")

        assert response.summary is not None and response.summary != ""
        assert isinstance(response.new_evidence, list)
        assert isinstance(response.updated_hypotheses, list)
        assert isinstance(response.suggested_next_steps, list)
        assert len(response.suggested_next_steps) >= 1

    def test_invoke_skill_response_has_all_fields(self):
        """invoke_skill response should have summary, evidence, hypotheses, next_steps."""
        session_id = _create_session()
        response = invoke_skill(session_id, "log-triage", {})

        assert response.summary is not None and response.summary != ""
        assert isinstance(response.new_evidence, list)
        assert isinstance(response.updated_hypotheses, list)
        assert isinstance(response.suggested_next_steps, list)
        assert len(response.suggested_next_steps) >= 1

    def test_failed_skill_response_still_has_all_fields(self):
        """Even on failure, response should have all required fields."""
        session_id = _create_session()
        response = invoke_skill(session_id, "failing-skill", {})

        assert response.summary is not None and response.summary != ""
        assert isinstance(response.new_evidence, list)
        assert isinstance(response.updated_hypotheses, list)
        assert isinstance(response.suggested_next_steps, list)
        assert len(response.suggested_next_steps) >= 1
