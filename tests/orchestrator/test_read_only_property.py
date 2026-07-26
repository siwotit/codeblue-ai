"""Property-based tests for read-only guarantee enforcement.

**Validates: Requirements 10.1, 10.2, 10.3, 10.5**

Property 18: Read-only guarantee
For any workflow execution, verify no MCP invocation produces a
write/create/update/delete against monitored systems.

Uses hypothesis to generate arbitrary operation names and verify
the read-only guard holds across all inputs.
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, assume, settings

from orchestrator.read_only_guard import (
    ALLOWED_OPERATIONS,
    READ_ONLY_MCPS,
    SLACK_ALLOWED_WRITES,
    WRITE_VERB_PREFIXES,
    READ_VERB_PREFIXES,
    ReadOnlyGuard,
    ReadOnlyViolationError,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Strategy for read-only MCP names
read_only_mcp_st = st.sampled_from(sorted(READ_ONLY_MCPS))

# Strategy for write verb prefixes
write_verb_st = st.sampled_from(sorted(WRITE_VERB_PREFIXES))

# Strategy for generating action suffixes (appended after a verb prefix)
action_suffix_st = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_"),
    min_size=1,
    max_size=30,
)

# Strategy for unknown MCP names (not in any known set)
known_mcps = sorted(READ_ONLY_MCPS | {"slack"})
unknown_mcp_st = st.text(
    alphabet=st.characters(whitelist_categories=("Ll",), whitelist_characters="_"),
    min_size=2,
    max_size=20,
).filter(lambda s: s not in READ_ONLY_MCPS and s != "slack" and "." not in s)

# Strategy for arbitrary action names
arbitrary_action_st = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_"),
    min_size=1,
    max_size=40,
)


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------


class TestReadOnlyGuaranteeProperty:
    """Property 18: Read-only guarantee.

    **Validates: Requirements 10.1, 10.2, 10.3, 10.5**
    """

    @given(mcp=read_only_mcp_st, write_verb=write_verb_st, suffix=action_suffix_st)
    @settings(max_examples=200)
    def test_write_operations_on_monitored_mcps_always_rejected(
        self, mcp: str, write_verb: str, suffix: str
    ):
        """For any operation on a read-only MCP starting with a write verb prefix,
        the guard MUST reject it.

        **Validates: Requirements 10.1, 10.2, 10.5**

        This proves that no matter what operation name is constructed using a
        write verb prefix on cloudwatch/grafana/kubernetes/aws, the guard will
        never allow it through.
        """
        guard = ReadOnlyGuard()
        operation = f"{mcp}.{write_verb}{suffix}"

        # The operation must either be rejected (raise) or not be allowed
        assert guard.is_allowed(operation) is False

    @given(operation=st.sampled_from(sorted(ALLOWED_OPERATIONS)))
    @settings(max_examples=200)
    def test_allowed_operations_have_no_write_verbs_on_read_only_mcps(
        self, operation: str
    ):
        """For any operation in the allowlist on a read-only MCP, it must NOT
        start with a write verb prefix.

        **Validates: Requirements 10.1, 10.2**

        This verifies that the allowlist itself is consistent — no write
        operations have been accidentally added for monitored systems.
        """
        mcp, action = operation.split(".", 1)
        if mcp in READ_ONLY_MCPS:
            action_lower = action.lower()
            for prefix in WRITE_VERB_PREFIXES:
                assert not action_lower.startswith(prefix), (
                    f"Allowlist contains write verb '{prefix}' for read-only "
                    f"MCP '{mcp}': {operation}"
                )

    @given(operation=st.sampled_from(sorted(ALLOWED_OPERATIONS)))
    @settings(max_examples=200)
    def test_any_passing_operation_is_not_a_write_on_monitored_systems(
        self, operation: str
    ):
        """For any operation that passes validation, verify it does NOT perform
        a write against a monitored (read-only) system.

        **Validates: Requirements 10.2, 10.3, 10.5**

        The only allowed writes are slack.postMessage and slack.updateMessage.
        All other operations that pass must be read-only.
        """
        guard = ReadOnlyGuard()
        mcp, action = operation.split(".", 1)

        # If it passes validation, it should be safe
        result = guard.is_allowed(operation)
        assert result is True

        # If on a read-only MCP, action must not start with a write verb
        if mcp in READ_ONLY_MCPS:
            action_lower = action.lower()
            for prefix in WRITE_VERB_PREFIXES:
                assert not action_lower.startswith(prefix), (
                    f"Operation '{operation}' passed validation but starts with "
                    f"write verb '{prefix}' on read-only MCP '{mcp}'"
                )

        # If it's a write-like operation, it must be an explicitly allowed Slack write
        action_lower = action.lower()
        is_write_like = any(action_lower.startswith(p) for p in WRITE_VERB_PREFIXES)
        if is_write_like:
            assert operation in SLACK_ALLOWED_WRITES, (
                f"Operation '{operation}' has a write verb but is not in "
                f"SLACK_ALLOWED_WRITES"
            )

    @given(unknown_mcp=unknown_mcp_st, action=arbitrary_action_st)
    @settings(max_examples=200)
    def test_unknown_mcp_always_rejected_fail_closed(
        self, unknown_mcp: str, action: str
    ):
        """For any operation on an unknown MCP, the guard MUST reject it
        (fail-closed behavior).

        **Validates: Requirements 10.5**

        This ensures that even if a new MCP is introduced or a typo is made,
        the guard does not accidentally permit operations on unrecognized systems.
        """
        guard = ReadOnlyGuard()
        operation = f"{unknown_mcp}.{action}"

        # Must not be in our known MCPs (ensured by filter in strategy)
        assume(unknown_mcp not in READ_ONLY_MCPS)
        assume(unknown_mcp != "slack")

        assert guard.is_allowed(operation) is False
