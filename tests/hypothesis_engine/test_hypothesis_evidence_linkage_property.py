"""Property-based test: Hypothesis evidence linkage.

**Validates: Requirements 6.4, 6.6**

For any hypothesis, verify at least one supporting evidence item with source reference
and one verification step. The validate_hypotheses_output function must reject hypotheses
missing supporting evidence, missing source references, or missing verification steps.
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

# Strategy for non-empty strings suitable for evidence fields
non_empty_text = st.text(
    alphabet=st.characters(categories=("L", "N", "P", "Z")),
    min_size=1,
    max_size=100,
)

# Strategy for a valid source reference
source_reference = st.text(
    alphabet=st.characters(categories=("L", "N", "P", "Z")),
    min_size=1,
    max_size=80,
).filter(lambda s: s.strip() != "")


# Strategy for a single valid evidence item with source
@st.composite
def valid_evidence_item(draw: st.DrawFn) -> dict:
    """Generate a valid evidence item with a non-empty source reference."""
    return {
        "claim": draw(non_empty_text),
        "source": draw(source_reference),
        "weight": draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)),
    }


# Strategy for an evidence item missing the 'source' field
@st.composite
def evidence_item_missing_source(draw: st.DrawFn) -> dict:
    """Generate an evidence item without a source reference."""
    item: dict = {
        "claim": draw(non_empty_text),
        "weight": draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)),
    }
    # Randomly either omit source entirely or set it to empty/falsy
    choice = draw(st.integers(min_value=0, max_value=2))
    if choice == 1:
        item["source"] = ""
    elif choice == 2:
        item["source"] = None
    # choice == 0: source key omitted entirely
    return item


# Strategy for a valid verification step
verification_step = st.text(
    alphabet=st.characters(categories=("L", "N", "P", "Z")),
    min_size=5,
    max_size=150,
).filter(lambda s: s.strip() != "")


# Strategy for a fully valid hypothesis (passes evidence linkage validation)
@st.composite
def valid_hypothesis_with_evidence(draw: st.DrawFn) -> dict:
    """Generate a single valid hypothesis with proper evidence linkage."""
    rank = draw(st.integers(min_value=1, max_value=10))
    confidence = draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False))

    # At least 1 supporting evidence item with source
    evidence_count = draw(st.integers(min_value=1, max_value=5))
    supporting_evidence = draw(
        st.lists(valid_evidence_item(), min_size=evidence_count, max_size=evidence_count)
    )

    # At least 1 verification step
    verification_count = draw(st.integers(min_value=1, max_value=5))
    verification_steps = draw(
        st.lists(verification_step, min_size=verification_count, max_size=verification_count)
    )

    return {
        "rank": rank,
        "title": f"Hypothesis {rank}",
        "description": f"Description for hypothesis {rank}",
        "confidence": confidence,
        "supportingEvidence": supporting_evidence,
        "contradictingEvidence": [
            {
                "claim": "No contradicting evidence found",
                "source": "hypothesis-engine: review",
                "weight": 0.0,
            }
        ],
        "suggestedVerification": verification_steps,
    }


# Strategy for a hypothesis with empty supportingEvidence
@st.composite
def hypothesis_with_empty_evidence(draw: st.DrawFn) -> dict:
    """Generate a hypothesis with an empty supportingEvidence array."""
    rank = draw(st.integers(min_value=1, max_value=10))
    confidence = draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False))

    return {
        "rank": rank,
        "title": f"Hypothesis {rank}",
        "description": f"Description for hypothesis {rank}",
        "confidence": confidence,
        "supportingEvidence": [],
        "contradictingEvidence": [
            {
                "claim": "No contradicting evidence found",
                "source": "hypothesis-engine: review",
                "weight": 0.0,
            }
        ],
        "suggestedVerification": ["Check logs for confirmation"],
    }


# Strategy for a hypothesis with evidence but missing source fields
@st.composite
def hypothesis_with_sourceless_evidence(draw: st.DrawFn) -> dict:
    """Generate a hypothesis where at least one evidence item lacks a source."""
    rank = draw(st.integers(min_value=1, max_value=10))
    confidence = draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False))

    # Include at least one evidence item missing source
    bad_evidence = draw(
        st.lists(evidence_item_missing_source(), min_size=1, max_size=3)
    )

    return {
        "rank": rank,
        "title": f"Hypothesis {rank}",
        "description": f"Description for hypothesis {rank}",
        "confidence": confidence,
        "supportingEvidence": bad_evidence,
        "contradictingEvidence": [
            {
                "claim": "No contradicting evidence found",
                "source": "hypothesis-engine: review",
                "weight": 0.0,
            }
        ],
        "suggestedVerification": ["Check logs for confirmation"],
    }


# Strategy for a hypothesis with empty suggestedVerification
@st.composite
def hypothesis_with_empty_verification(draw: st.DrawFn) -> dict:
    """Generate a hypothesis with an empty suggestedVerification array."""
    rank = draw(st.integers(min_value=1, max_value=10))
    confidence = draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False))

    return {
        "rank": rank,
        "title": f"Hypothesis {rank}",
        "description": f"Description for hypothesis {rank}",
        "confidence": confidence,
        "supportingEvidence": [
            {
                "claim": "Evidence for hypothesis",
                "source": "metric-baseline: cpu-utilization",
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
        "suggestedVerification": [],
    }


class TestHypothesisEvidenceLinkage:
    """Property 13: Hypothesis evidence linkage."""

    @given(hypothesis_data=valid_hypothesis_with_evidence())
    @settings(max_examples=500)
    def test_valid_evidence_passes_validation(self, hypothesis_data: dict) -> None:
        """For any hypothesis with >=1 supporting evidence item with source reference
        AND >=1 verification step, validation produces no evidence-linkage errors.

        **Validates: Requirements 6.4, 6.6**
        """
        data = {"hypotheses": [hypothesis_data]}
        errors = validate_hypotheses_output(data)

        # Filter to evidence and verification related errors
        evidence_errors = [
            e for e in errors
            if "supporting evidence" in e.lower()
            or "source" in e.lower()
            or "verification" in e.lower()
        ]

        assert evidence_errors == [], (
            f"Valid hypothesis with evidence and verification steps should have no "
            f"evidence-linkage errors, but got: {evidence_errors}"
        )

    @given(hypothesis_data=hypothesis_with_empty_evidence())
    @settings(max_examples=500)
    def test_empty_supporting_evidence_fails_validation(self, hypothesis_data: dict) -> None:
        """For any hypothesis with empty supportingEvidence array,
        validate_hypotheses_output reports the violation.

        **Validates: Requirement 6.4**
        """
        data = {"hypotheses": [hypothesis_data]}
        errors = validate_hypotheses_output(data)

        evidence_errors = [
            e for e in errors
            if "supporting evidence" in e.lower()
        ]

        assert len(evidence_errors) > 0, (
            f"Hypothesis with empty supportingEvidence should trigger validation error. "
            f"Errors returned: {errors}"
        )

    @given(hypothesis_data=hypothesis_with_sourceless_evidence())
    @settings(max_examples=500)
    def test_evidence_missing_source_fails_validation(self, hypothesis_data: dict) -> None:
        """For any hypothesis where evidence items lack source references,
        validate_hypotheses_output reports the violation.

        **Validates: Requirement 6.4**
        """
        data = {"hypotheses": [hypothesis_data]}
        errors = validate_hypotheses_output(data)

        source_errors = [
            e for e in errors
            if "source" in e.lower()
        ]

        assert len(source_errors) > 0, (
            f"Hypothesis with evidence missing 'source' field should trigger validation error. "
            f"Evidence: {hypothesis_data['supportingEvidence']}, errors: {errors}"
        )

    @given(hypothesis_data=hypothesis_with_empty_verification())
    @settings(max_examples=500)
    def test_empty_verification_steps_fails_validation(self, hypothesis_data: dict) -> None:
        """For any hypothesis with empty suggestedVerification array,
        validate_hypotheses_output reports the violation.

        **Validates: Requirement 6.6**
        """
        data = {"hypotheses": [hypothesis_data]}
        errors = validate_hypotheses_output(data)

        verification_errors = [
            e for e in errors
            if "verification" in e.lower()
        ]

        assert len(verification_errors) > 0, (
            f"Hypothesis with empty suggestedVerification should trigger validation error. "
            f"Errors returned: {errors}"
        )
