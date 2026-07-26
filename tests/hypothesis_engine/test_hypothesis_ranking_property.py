"""Property-based test: Hypothesis ranking validity.

**Validates: Requirements 6.2, 6.7**

For any set of hypotheses, verify strictly descending confidence and unique ranks.
The validate_hypotheses_output function must reject duplicate ranks and non-descending
confidence ordering.
"""

from __future__ import annotations

import sys

from hypothesis import given, settings, assume
from hypothesis import strategies as st

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "skills"))
sys.path.insert(
    0,
    str(
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "skills"
        / "hypothesis-engine"
    ),
)

from hypothesis_engine import validate_hypotheses_output


# --- Strategies ---

# Strategy for confidence values: floats in valid range [0.0, 1.0]
confidence_strategy = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)

# Strategy for a single valid hypothesis with given rank and confidence
def make_hypothesis(rank: int, confidence: float) -> dict:
    """Build a fully valid hypothesis dict with the given rank and confidence."""
    return {
        "rank": rank,
        "title": f"Hypothesis rank {rank}",
        "description": f"Description for hypothesis at rank {rank} with confidence {confidence:.4f}",
        "confidence": confidence,
        "supportingEvidence": [
            {
                "claim": f"Evidence supporting hypothesis {rank}",
                "source": "metric-baseline: test-metric",
                "weight": 0.8,
            }
        ],
        "contradictingEvidence": [
            {
                "claim": "No contradicting evidence found",
                "source": "hypothesis-engine: review",
                "weight": 0.0,
            }
        ],
        "suggestedVerification": [f"Verify hypothesis {rank} by checking logs"],
    }


# Strategy to generate a valid sorted list of hypotheses (1-10 hypotheses, unique ranks,
# strictly descending confidence)
@st.composite
def valid_hypotheses_list(draw: st.DrawFn) -> list[dict]:
    """Generate a list of 1-10 hypotheses with unique ranks and strictly descending confidence."""
    count = draw(st.integers(min_value=1, max_value=10))

    # Generate unique confidence values and sort descending
    confidences = draw(
        st.lists(
            confidence_strategy,
            min_size=count,
            max_size=count,
            unique=True,
        )
    )
    confidences.sort(reverse=True)

    # Assign sequential ranks starting from 1
    hypotheses = []
    for i in range(count):
        hypotheses.append(make_hypothesis(rank=i + 1, confidence=confidences[i]))

    return hypotheses


# Strategy for hypotheses with duplicate ranks
@st.composite
def hypotheses_with_duplicate_ranks(draw: st.DrawFn) -> list[dict]:
    """Generate 2-10 hypotheses where at least two share the same rank."""
    count = draw(st.integers(min_value=2, max_value=10))

    # Generate confidences in descending order
    confidences = draw(
        st.lists(
            confidence_strategy,
            min_size=count,
            max_size=count,
            unique=True,
        )
    )
    confidences.sort(reverse=True)

    # Assign ranks, but duplicate at least one
    ranks = list(range(1, count + 1))
    # Pick a random index (not the first) and make its rank duplicate an earlier one
    dup_idx = draw(st.integers(min_value=1, max_value=count - 1))
    source_idx = draw(st.integers(min_value=0, max_value=dup_idx - 1))
    ranks[dup_idx] = ranks[source_idx]

    hypotheses = []
    for i in range(count):
        hypotheses.append(make_hypothesis(rank=ranks[i], confidence=confidences[i]))

    return hypotheses


# Strategy for hypotheses with non-descending confidence
@st.composite
def hypotheses_with_non_descending_confidence(draw: st.DrawFn) -> list[dict]:
    """Generate 2-10 hypotheses where confidence is NOT strictly descending."""
    count = draw(st.integers(min_value=2, max_value=10))

    # Generate confidence values
    confidences = draw(
        st.lists(
            confidence_strategy,
            min_size=count,
            max_size=count,
        )
    )

    # Ensure at least one pair violates descending order
    # Sort descending, then swap one pair to create a violation
    confidences.sort(reverse=True)
    swap_idx = draw(st.integers(min_value=0, max_value=count - 2))
    # Make the element after swap_idx strictly greater than the element at swap_idx
    # This guarantees a non-descending violation
    confidences[swap_idx + 1] = min(confidences[swap_idx] + 0.01, 1.0)
    # If we hit the cap, bump up the later one slightly differently
    if confidences[swap_idx + 1] <= confidences[swap_idx]:
        # They're equal or still descending — force violation by setting later > earlier
        confidences[swap_idx] = max(confidences[swap_idx + 1] - 0.01, 0.0)

    # Final guarantee: confirm violation exists
    assume(any(
        confidences[i + 1] > confidences[i] for i in range(len(confidences) - 1)
    ))

    hypotheses = []
    for i in range(count):
        hypotheses.append(make_hypothesis(rank=i + 1, confidence=confidences[i]))

    return hypotheses


class TestHypothesisRankingValidity:
    """Property 12: Hypothesis ranking validity."""

    @given(hypotheses=valid_hypotheses_list())
    @settings(max_examples=500)
    def test_valid_hypotheses_pass_validation(self, hypotheses: list[dict]) -> None:
        """For any valid set of hypotheses with unique ranks and descending confidence,
        validate_hypotheses_output returns no errors.

        **Validates: Requirements 6.2, 6.7**
        """
        data = {"hypotheses": hypotheses}
        errors = validate_hypotheses_output(data)

        # Filter to only ranking/confidence related errors
        ranking_errors = [
            e for e in errors
            if "rank" in e.lower() or "descending" in e.lower() or "duplicate" in e.lower()
        ]

        assert ranking_errors == [], (
            f"Valid hypotheses (unique ranks, descending confidence) should have no "
            f"ranking-related validation errors, but got: {ranking_errors}"
        )

    @given(hypotheses=hypotheses_with_duplicate_ranks())
    @settings(max_examples=500)
    def test_duplicate_ranks_detected(self, hypotheses: list[dict]) -> None:
        """For any set of hypotheses with duplicate ranks,
        validate_hypotheses_output reports the violation.

        **Validates: Requirement 6.7**
        """
        data = {"hypotheses": hypotheses}
        errors = validate_hypotheses_output(data)

        duplicate_errors = [e for e in errors if "duplicate rank" in e.lower()]
        assert len(duplicate_errors) > 0, (
            f"Hypotheses with duplicate ranks should trigger validation error. "
            f"Ranks: {[h['rank'] for h in hypotheses]}, errors: {errors}"
        )

    @given(hypotheses=hypotheses_with_non_descending_confidence())
    @settings(max_examples=500)
    def test_non_descending_confidence_detected(self, hypotheses: list[dict]) -> None:
        """For any set of hypotheses where confidence is not strictly descending,
        validate_hypotheses_output reports the violation.

        **Validates: Requirement 6.2**
        """
        data = {"hypotheses": hypotheses}
        errors = validate_hypotheses_output(data)

        ordering_errors = [e for e in errors if "descending order" in e.lower()]
        assert len(ordering_errors) > 0, (
            f"Hypotheses with non-descending confidence should trigger validation error. "
            f"Confidences: {[h['confidence'] for h in hypotheses]}, errors: {errors}"
        )

    @given(
        count=st.integers(min_value=1, max_value=10),
    )
    @settings(max_examples=200)
    def test_sequential_ranks_are_unique(self, count: int) -> None:
        """For any generated set with sequential ranks 1..N, all ranks are unique
        and validation passes the uniqueness check.

        **Validates: Requirement 6.7**
        """
        # Build hypotheses with sequential ranks and linearly decreasing confidence
        confidences = [1.0 - (i * 0.09) for i in range(count)]
        hypotheses = [make_hypothesis(rank=i + 1, confidence=confidences[i]) for i in range(count)]

        data = {"hypotheses": hypotheses}
        errors = validate_hypotheses_output(data)

        rank_errors = [e for e in errors if "duplicate rank" in e.lower()]
        assert rank_errors == [], (
            f"Sequential ranks 1..{count} should never produce duplicate rank errors, "
            f"but got: {rank_errors}"
        )
