"""Property-based test for conversation context preservation across follow-ups.

**Property 24: Conversation context preservation across follow-ups**
**Validates: Requirements 13.4, 13.9**

For any session with N turns, verify follow-up produces N+1 turns with
full prior context preserved. Specifically:
1. Conversation history length increases by 2 (user turn + agent turn) per follow-up
2. All prior turns are preserved (content matches, order unchanged)
3. The new user turn contains the exact prompt text
4. The initial turn is always preserved across all follow-ups
5. No conversation history entry is ever removed or modified
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from orchestrator.interactive_triage import (
    clear_sessions,
    follow_up,
    register_skill,
    start_investigation,
    _get_session,
)
from skills.shared.config import CodeBlueConfig


# ---------------------------------------------------------------------------
# Mock skills
# ---------------------------------------------------------------------------


def _mock_metric_skill(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": "Metric baseline result",
        "evidence_items": [
            {"claim": "CPU at 85%", "source": "metric-baseline", "weight": 0.7}
        ],
    }


def _mock_log_skill(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": "Log triage result",
        "evidence_items": [
            {"claim": "Found errors in logs", "source": "log-triage", "weight": 0.6}
        ],
    }


def _mock_k8s_skill(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": "Cluster health result",
        "evidence_items": [
            {"claim": "Cluster healthy", "source": "k8s-cluster-health", "weight": 0.5}
        ],
    }


def _mock_deploy_skill(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": "Deploy correlation result",
        "evidence_items": [
            {"claim": "Recent deploy found", "source": "deploy-correlation", "weight": 0.65}
        ],
    }


def _mock_hypothesis_skill(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": "Hypothesis generated",
        "evidence_items": [
            {"claim": "Likely deploy issue", "source": "hypothesis-engine", "weight": 0.8}
        ],
    }


def _mock_pod_skill(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": "Pod failure result",
        "evidence_items": [
            {"claim": "Pods crashing", "source": "pod-failure-triage", "weight": 0.75}
        ],
    }


def _mock_node_skill(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": "Node condition result",
        "evidence_items": [
            {"claim": "Node healthy", "source": "node-condition-check", "weight": 0.5}
        ],
    }


# ---------------------------------------------------------------------------
# Setup / Teardown Helper
# ---------------------------------------------------------------------------


def _setup_skills() -> None:
    """Register mock skills and clear session state."""
    clear_sessions()
    register_skill("metric-baseline", _mock_metric_skill)
    register_skill("log-triage", _mock_log_skill)
    register_skill("k8s-cluster-health", _mock_k8s_skill)
    register_skill("deploy-correlation", _mock_deploy_skill)
    register_skill("hypothesis-engine", _mock_hypothesis_skill)
    register_skill("pod-failure-triage", _mock_pod_skill)
    register_skill("node-condition-check", _mock_node_skill)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Generate follow-up prompt sequences of length 1 to 5.
# Use printable text to avoid issues with non-printable chars, and ensure
# non-empty strings so the follow-up is meaningful.
follow_up_prompts_strategy = st.lists(
    st.text(
        alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
        min_size=1,
        max_size=80,
    ),
    min_size=1,
    max_size=5,
)


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------


@settings(max_examples=50, deadline=30000)
@given(follow_up_prompts=follow_up_prompts_strategy)
def test_conversation_history_grows_by_two_per_follow_up(
    follow_up_prompts: list[str],
) -> None:
    """Property: After each follow_up call, conversation history length
    increases by exactly 2 (one user turn + one agent turn).

    **Validates: Requirements 13.4, 13.9**
    """
    _setup_skills()

    config = CodeBlueConfig(region="us-east-1")
    session, _, _ = start_investigation("prod-cluster throwing 503s", config=config)
    session_id = session.session_id

    # Initial state: 1 turn (the user prompt from start_investigation)
    current_session = _get_session(session_id)
    expected_length = len(current_session.conversation_history)

    for prompt in follow_up_prompts:
        follow_up(session_id, prompt)
        expected_length += 2  # user turn + agent turn

        current_session = _get_session(session_id)
        assert len(current_session.conversation_history) == expected_length, (
            f"Expected {expected_length} turns but got "
            f"{len(current_session.conversation_history)} after follow-up: {prompt!r}"
        )


@settings(max_examples=50, deadline=30000)
@given(follow_up_prompts=follow_up_prompts_strategy)
def test_prior_turns_preserved_after_follow_ups(
    follow_up_prompts: list[str],
) -> None:
    """Property: All prior turns are preserved across follow-ups — their
    content remains unchanged and their order is maintained.

    **Validates: Requirements 13.4, 13.9**
    """
    _setup_skills()

    config = CodeBlueConfig(region="us-east-1")
    session, _, _ = start_investigation("prod-cluster throwing 503s", config=config)
    session_id = session.session_id

    # Track the full history snapshot after each step
    for prompt in follow_up_prompts:
        # Capture conversation state BEFORE this follow-up
        session_before = _get_session(session_id)
        history_before = [
            (turn.role, turn.content)
            for turn in session_before.conversation_history
        ]

        follow_up(session_id, prompt)

        # After the follow-up, all previous turns must remain intact
        session_after = _get_session(session_id)
        history_after = [
            (turn.role, turn.content)
            for turn in session_after.conversation_history
        ]

        # The prefix of history_after must match history_before exactly
        for i, (role_before, content_before) in enumerate(history_before):
            assert history_after[i] == (role_before, content_before), (
                f"Turn {i} was modified after follow-up {prompt!r}: "
                f"was {(role_before, content_before)}, "
                f"now {history_after[i]}"
            )


@settings(max_examples=50, deadline=30000)
@given(follow_up_prompts=follow_up_prompts_strategy)
def test_new_user_turn_contains_exact_prompt(
    follow_up_prompts: list[str],
) -> None:
    """Property: After each follow_up, the new user turn contains the
    exact prompt text that was submitted.

    **Validates: Requirements 13.4, 13.9**
    """
    _setup_skills()

    config = CodeBlueConfig(region="us-east-1")
    session, _, _ = start_investigation("prod-cluster throwing 503s", config=config)
    session_id = session.session_id

    for prompt in follow_up_prompts:
        session_before = _get_session(session_id)
        turns_before = len(session_before.conversation_history)

        follow_up(session_id, prompt)

        session_after = _get_session(session_id)
        # The user turn is at index turns_before (0-indexed, right after previous turns)
        user_turn = session_after.conversation_history[turns_before]
        assert user_turn.role == "user"
        assert user_turn.content == prompt, (
            f"Expected user turn content to be {prompt!r}, "
            f"got {user_turn.content!r}"
        )


@settings(max_examples=50, deadline=30000)
@given(follow_up_prompts=follow_up_prompts_strategy)
def test_initial_turn_always_preserved(
    follow_up_prompts: list[str],
) -> None:
    """Property: The initial conversation turn from start_investigation
    is always preserved across all follow-ups — never removed or modified.

    **Validates: Requirements 13.4, 13.9**
    """
    _setup_skills()

    initial_prompt = "prod-cluster throwing 503s"
    config = CodeBlueConfig(region="us-east-1")
    session, _, _ = start_investigation(initial_prompt, config=config)
    session_id = session.session_id

    # Capture the initial turn
    initial_session = _get_session(session_id)
    initial_turn_content = initial_session.conversation_history[0].content
    initial_turn_role = initial_session.conversation_history[0].role

    for prompt in follow_up_prompts:
        follow_up(session_id, prompt)

        current_session = _get_session(session_id)
        first_turn = current_session.conversation_history[0]

        assert first_turn.role == initial_turn_role, (
            f"Initial turn role changed from {initial_turn_role!r} to {first_turn.role!r}"
        )
        assert first_turn.content == initial_turn_content, (
            f"Initial turn content changed from {initial_turn_content!r} to {first_turn.content!r}"
        )


@settings(max_examples=50, deadline=30000)
@given(follow_up_prompts=follow_up_prompts_strategy)
def test_no_history_entry_removed_or_modified(
    follow_up_prompts: list[str],
) -> None:
    """Property: No conversation history entry is ever removed or modified
    across the entire sequence of follow-ups.

    **Validates: Requirements 13.4, 13.9**
    """
    _setup_skills()

    config = CodeBlueConfig(region="us-east-1")
    session, _, _ = start_investigation("prod-cluster throwing 503s", config=config)
    session_id = session.session_id

    # After each follow-up, take a snapshot and compare with next iteration
    snapshots: list[list[tuple[str, str]]] = []

    # Initial snapshot
    current = _get_session(session_id)
    snapshots.append([(t.role, t.content) for t in current.conversation_history])

    for prompt in follow_up_prompts:
        follow_up(session_id, prompt)

        current = _get_session(session_id)
        new_snapshot = [(t.role, t.content) for t in current.conversation_history]
        snapshots.append(new_snapshot)

    # Verify: each snapshot is a prefix of the next
    for i in range(len(snapshots) - 1):
        earlier = snapshots[i]
        later = snapshots[i + 1]

        # Later must be strictly longer (follow-up adds turns)
        assert len(later) > len(earlier), (
            f"Snapshot {i+1} not longer than snapshot {i}: "
            f"{len(later)} vs {len(earlier)}"
        )

        # Earlier must be an exact prefix of later
        for j, (role, content) in enumerate(earlier):
            assert later[j] == (role, content), (
                f"Turn {j} was modified between snapshot {i} and {i+1}: "
                f"was {(role, content)}, now {later[j]}"
            )
