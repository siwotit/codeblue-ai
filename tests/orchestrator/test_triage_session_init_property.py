"""Property-based tests for TriageSession initialization completeness.

**Validates: Requirement 13.3**

Property 22: TriageSession initialization completeness
Verify session has unique ID, empty accumulators, and at least one skill
invocation triggered for any arbitrary description string.

Uses hypothesis to generate arbitrary description strings and verify the
session is always initialized correctly.
"""

from __future__ import annotations

import uuid

import hypothesis.strategies as st
from hypothesis import given, settings

from orchestrator.interactive_triage import clear_sessions, start_investigation
from skills.shared.config import CodeBlueConfig


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Arbitrary description strings — use text() to cover wide input space
description_st = st.text(min_size=0, max_size=500)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_valid_uuid(value: str) -> bool:
    """Check if a string is a valid UUID format."""
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------


class TestTriageSessionInitCompleteness:
    """Property 22: TriageSession initialization completeness.

    **Validates: Requirement 13.3**
    """

    @given(description=description_st)
    @settings(max_examples=200)
    def test_session_has_non_empty_unique_uuid_session_id(
        self, description: str
    ) -> None:
        """For any description, session_id is a non-empty valid UUID.

        **Validates: Requirements 13.3**
        """
        clear_sessions()
        config = CodeBlueConfig(region="us-east-1")
        session, _, _ = start_investigation(description, config=config)

        assert session.session_id != ""
        assert _is_valid_uuid(session.session_id)

    @given(description=description_st)
    @settings(max_examples=200)
    def test_evidence_accumulator_starts_empty(self, description: str) -> None:
        """For any description, evidence_accumulator starts as an empty list.

        **Validates: Requirements 13.3**
        """
        clear_sessions()
        config = CodeBlueConfig(region="us-east-1")
        session, _, _ = start_investigation(description, config=config)

        assert session.evidence_accumulator == []

    @given(description=description_st)
    @settings(max_examples=200)
    def test_active_hypotheses_starts_empty(self, description: str) -> None:
        """For any description, active_hypotheses starts as an empty list.

        **Validates: Requirements 13.3**
        """
        clear_sessions()
        config = CodeBlueConfig(region="us-east-1")
        session, _, _ = start_investigation(description, config=config)

        assert session.active_hypotheses == []

    @given(description=description_st)
    @settings(max_examples=200)
    def test_conversation_history_has_exactly_one_user_turn(
        self, description: str
    ) -> None:
        """For any description, conversation_history has exactly one turn
        (the user prompt) with role='user' and matching content.

        **Validates: Requirements 13.3**
        """
        clear_sessions()
        config = CodeBlueConfig(region="us-east-1")
        session, _, _ = start_investigation(description, config=config)

        assert len(session.conversation_history) == 1
        turn = session.conversation_history[0]
        assert turn.role == "user"
        assert turn.content == description

    @given(description=description_st)
    @settings(max_examples=200)
    def test_at_least_one_initial_skill_returned(self, description: str) -> None:
        """For any description, at least one skill is returned for invocation.

        **Validates: Requirements 13.3**
        """
        clear_sessions()
        config = CodeBlueConfig(region="us-east-1")
        _, _, skills = start_investigation(description, config=config)

        assert len(skills) >= 1

    @given(
        desc1=st.text(min_size=0, max_size=200),
        desc2=st.text(min_size=0, max_size=200),
    )
    @settings(max_examples=200)
    def test_consecutive_calls_produce_different_session_ids(
        self, desc1: str, desc2: str
    ) -> None:
        """Two consecutive calls to start_investigation always produce
        different session IDs, regardless of input descriptions.

        **Validates: Requirements 13.3**
        """
        clear_sessions()
        config = CodeBlueConfig(region="us-east-1")
        session1, _, _ = start_investigation(desc1, config=config)
        session2, _, _ = start_investigation(desc2, config=config)

        assert session1.session_id != session2.session_id

    @given(description=description_st)
    @settings(max_examples=200)
    def test_start_investigation_never_crashes(self, description: str) -> None:
        """For any description string (including empty, unicode, special chars),
        start_investigation must never raise an unhandled exception.

        **Validates: Requirements 13.3**
        """
        clear_sessions()
        config = CodeBlueConfig(region="us-east-1")
        # This should not raise — if it does, hypothesis will report it
        session, annotations, skills = start_investigation(
            description, config=config
        )
        # Basic structural integrity checks
        assert session is not None
        assert annotations is not None
        assert skills is not None
