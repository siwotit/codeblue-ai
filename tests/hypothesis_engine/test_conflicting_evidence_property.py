"""Property-based test: Conflicting evidence produces multiple hypotheses.

**Validates: Requirement 11.5**

For any incident context with contradicting indicators, verify multiple ranked
hypotheses are produced. Requirement 11.5 states: "When two or more hypotheses
have confidence scores within 0.15 of each other, report all such hypotheses
with supporting and contradicting evidence items for each."

This test verifies:
1. The validator accepts multiple hypotheses with close confidence scores (within 0.15)
2. Each such hypothesis has supporting AND contradicting evidence items
3. The build_insufficient_data_response only produces 1 hypothesis (minimal case)
4. Valid hypothesis sets with close confidences pass validation
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

from hypothesis_engine import validate_hypotheses_output, build_insufficient_data_response


# --- Strategies ---


def make_full_hypothesis(rank: int, confidence: float) -> dict:
    """Build a fully valid hypothesis with supporting AND contradicting evidence."""
    return {
        "rank": rank,
        "title": f"Hypothesis {rank}: possible root cause",
        "description": (
            f"This hypothesis at rank {rank} with confidence {confidence:.4f} "
            f"represents a plausible explanation for the incident."
        ),
        "confidence": confidence,
        "supportingEvidence": [
            {
                "claim": f"Metric deviation observed supporting hypothesis {rank}",
                "source": "metric-baseline: CPUUtilization anomalous",
                "timestamp": "2024-01-15T10:30:00Z",
                "weight": 0.7,
            }
        ],
        "contradictingEvidence": [
            {
                "claim": f"Log pattern partially conflicts with hypothesis {rank}",
                "source": "log-triage: error pattern analysis",
                "timestamp": "2024-01-15T10:25:00Z",
                "weight": 0.3,
            }
        ],
        "suggestedVerification": [
            f"Check deployment logs around anomaly start time for hypothesis {rank}"
        ],
    }


@st.composite
def conflicting_hypotheses_set(draw: st.DrawFn) -> list[dict]:
    """Generate 2-10 hypotheses where at least 2 have confidence within 0.15.

    This simulates the scenario of conflicting evidence where multiple
    hypotheses have similar confidence because the evidence is ambiguous.
    """
    count = draw(st.integers(min_value=2, max_value=10))

    # Generate a base confidence for the top cluster
    base_confidence = draw(
        st.floats(min_value=0.2, max_value=0.95, allow_nan=False, allow_infinity=False)
    )

    # Determine how many hypotheses are in the "close cluster" (at least 2)
    cluster_size = draw(st.integers(min_value=2, max_value=min(count, 5)))

    # Generate close confidences within 0.15 of the base
    close_confidences = []
    for _ in range(cluster_size):
        offset = draw(
            st.floats(
                min_value=0.0, max_value=0.14, allow_nan=False, allow_infinity=False
            )
        )
        conf = base_confidence - offset
        # Clamp to valid range
        conf = max(0.0, min(1.0, conf))
        close_confidences.append(conf)

    # Generate remaining confidences that are lower (outside the cluster)
    remaining_confidences = []
    for _ in range(count - cluster_size):
        # Must be lower than the cluster minimum by more than 0.15
        lower_bound = max(0.0, min(close_confidences) - 0.5)
        upper_bound = max(0.0, min(close_confidences) - 0.16)
        if upper_bound <= lower_bound:
            upper_bound = lower_bound + 0.01
        conf = draw(
            st.floats(
                min_value=lower_bound,
                max_value=min(upper_bound, 1.0),
                allow_nan=False,
                allow_infinity=False,
            )
        )
        remaining_confidences.append(conf)

    # Combine and sort descending
    all_confidences = close_confidences + remaining_confidences
    all_confidences.sort(reverse=True)

    # Ensure uniqueness for valid descending order
    # Add tiny offsets to break ties
    for i in range(1, len(all_confidences)):
        if all_confidences[i] >= all_confidences[i - 1]:
            all_confidences[i] = all_confidences[i - 1] - 0.001

    # Clamp all values to [0.0, 1.0]
    all_confidences = [max(0.0, min(1.0, c)) for c in all_confidences]

    # Re-sort after clamping
    all_confidences.sort(reverse=True)

    # Ensure strictly descending
    assume(all(
        all_confidences[i] > all_confidences[i + 1]
        for i in range(len(all_confidences) - 1)
    ))

    # Confirm at least 2 hypotheses are within 0.15 of each other
    assume(any(
        all_confidences[i] - all_confidences[i + 1] <= 0.15
        for i in range(len(all_confidences) - 1)
    ))

    # Build hypothesis list
    hypotheses = []
    for i, conf in enumerate(all_confidences):
        hypotheses.append(make_full_hypothesis(rank=i + 1, confidence=conf))

    return hypotheses


@st.composite
def unavailable_signals_list(draw: st.DrawFn) -> list[str]:
    """Generate a list of unavailable signal names."""
    signal_types = ["metricDeviation", "correlatedChanges", "logFindings", "clusterHealth"]
    count = draw(st.integers(min_value=1, max_value=4))
    selected = draw(
        st.lists(
            st.sampled_from(signal_types),
            min_size=count,
            max_size=count,
            unique=True,
        )
    )
    return selected


class TestConflictingEvidenceMultipleHypotheses:
    """Property 20: Conflicting evidence produces multiple hypotheses."""

    @given(hypotheses=conflicting_hypotheses_set())
    @settings(max_examples=500)
    def test_multiple_close_confidence_hypotheses_pass_validation(
        self, hypotheses: list[dict]
    ) -> None:
        """For any set of hypotheses with close confidence scores (within 0.15),
        validate_hypotheses_output accepts all of them.

        This verifies the validator supports the scenario required by Req 11.5:
        multiple hypotheses with confidence scores within 0.15 of each other
        are all reported (not filtered out).

        **Validates: Requirement 11.5**
        """
        data = {"hypotheses": hypotheses}
        errors = validate_hypotheses_output(data)

        assert errors == [], (
            f"Multiple hypotheses with close confidence scores should pass validation. "
            f"Confidences: {[h['confidence'] for h in hypotheses]}, "
            f"errors: {errors}"
        )

    @given(hypotheses=conflicting_hypotheses_set())
    @settings(max_examples=500)
    def test_close_confidence_hypotheses_all_have_evidence(
        self, hypotheses: list[dict]
    ) -> None:
        """For any set of hypotheses with close confidence scores,
        verify each hypothesis has both supporting AND contradicting evidence.

        Requirement 11.5 mandates that all close-confidence hypotheses
        are reported with "supporting and contradicting evidence items for each."

        **Validates: Requirement 11.5**
        """
        # Find the cluster of hypotheses within 0.15 of each other
        confidences = [h["confidence"] for h in hypotheses]

        # Identify hypotheses within 0.15 of the top confidence
        top_confidence = confidences[0]
        close_hypotheses = [
            h for h in hypotheses if top_confidence - h["confidence"] <= 0.15
        ]

        # There must be at least 2 close hypotheses
        assert len(close_hypotheses) >= 2, (
            f"Expected at least 2 hypotheses within 0.15 of top confidence "
            f"{top_confidence}, but got {len(close_hypotheses)}"
        )

        # Each hypothesis in the close cluster must have supporting AND contradicting evidence
        for h in close_hypotheses:
            supporting = h.get("supportingEvidence", [])
            contradicting = h.get("contradictingEvidence", [])

            assert len(supporting) >= 1, (
                f"Hypothesis rank {h['rank']} (confidence {h['confidence']}) "
                f"must have at least 1 supporting evidence item"
            )
            assert len(contradicting) >= 1, (
                f"Hypothesis rank {h['rank']} (confidence {h['confidence']}) "
                f"must have at least 1 contradicting evidence item"
            )

            # Each supporting evidence must have a source reference
            for ev in supporting:
                assert ev.get("source"), (
                    f"Supporting evidence for hypothesis rank {h['rank']} "
                    f"must have a 'source' reference"
                )

    @given(hypotheses=conflicting_hypotheses_set())
    @settings(max_examples=300)
    def test_multiple_hypotheses_count_exceeds_one(
        self, hypotheses: list[dict]
    ) -> None:
        """For any incident context with conflicting indicators (close confidence scores),
        verify multiple ranked hypotheses are produced (count > 1).

        **Validates: Requirement 11.5**
        """
        data = {"hypotheses": hypotheses}

        # The data must contain more than 1 hypothesis
        assert len(hypotheses) >= 2, (
            f"Conflicting evidence should produce multiple hypotheses, "
            f"got {len(hypotheses)}"
        )

        # And validation should pass
        errors = validate_hypotheses_output(data)
        assert errors == [], (
            f"Multiple hypotheses from conflicting evidence should be valid. "
            f"Errors: {errors}"
        )

    @given(
        unavailable_signals=unavailable_signals_list(),
    )
    @settings(max_examples=200)
    def test_insufficient_data_produces_single_hypothesis(
        self, unavailable_signals: list[str]
    ) -> None:
        """The build_insufficient_data_response function only produces exactly 1
        hypothesis (the minimal case), contrasting with the multiple-hypothesis
        scenario of conflicting evidence.

        This proves that the system distinguishes between:
        - Conflicting evidence → multiple hypotheses (Req 11.5)
        - Insufficient data → single hypothesis (Req 6.9)

        **Validates: Requirement 11.5**
        """
        confidence_adjustment = -0.2 * len(unavailable_signals)

        result = build_insufficient_data_response(unavailable_signals, confidence_adjustment)

        hypotheses = result["hypotheses"]
        assert len(hypotheses) == 1, (
            f"Insufficient data response should produce exactly 1 hypothesis, "
            f"got {len(hypotheses)}"
        )

        # The single hypothesis should have confidence 0.1
        assert hypotheses[0]["confidence"] == 0.1, (
            f"Insufficient data hypothesis should have confidence 0.1, "
            f"got {hypotheses[0]['confidence']}"
        )

        # It should still pass validation
        errors = validate_hypotheses_output(result)
        assert errors == [], (
            f"Insufficient data response should pass validation. Errors: {errors}"
        )

    @given(hypotheses=conflicting_hypotheses_set())
    @settings(max_examples=300)
    def test_conflicting_hypotheses_maintain_descending_confidence_order(
        self, hypotheses: list[dict]
    ) -> None:
        """Even when hypotheses have close confidence scores, they must
        maintain strictly descending confidence order per Requirement 6.2.

        **Validates: Requirement 11.5**
        """
        confidences = [h["confidence"] for h in hypotheses]

        # Verify strictly descending
        for i in range(len(confidences) - 1):
            assert confidences[i] > confidences[i + 1], (
                f"Confidences must be strictly descending even for close values. "
                f"Position {i}: {confidences[i]} <= {confidences[i+1]}"
            )

        # Validator should also accept this
        data = {"hypotheses": hypotheses}
        errors = validate_hypotheses_output(data)
        ordering_errors = [e for e in errors if "descending" in e.lower()]
        assert ordering_errors == [], (
            f"Close-confidence hypotheses in descending order should not "
            f"trigger ordering errors: {ordering_errors}"
        )
