"""Property-based test: Monotonic severity classification.

**Validates: Requirement 2.5**

For any two deviation factors where d2 > d1, verify classification(d2) >= classification(d1).
The classify_deviation function must produce monotonically non-decreasing severity as
the deviation factor increases (using absolute values).
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
        / "metric-baseline"
    ),
)

from metric_baseline import classify_deviation, _SEVERITY_ORDER
from shared.models import DeviationClassification


# Strategy for deviation factors: non-negative floats representing absolute standard deviations.
# We use non-negative values since classify_deviation takes abs() internally.
deviation_factor_strategy = st.floats(
    min_value=0.0, max_value=1000.0, allow_nan=False, allow_infinity=False
)


class TestMonotonicSeverityClassification:
    """Property 3: Monotonic severity classification."""

    @given(
        d1=deviation_factor_strategy,
        d2=deviation_factor_strategy,
    )
    @settings(max_examples=1000)
    def test_higher_deviation_never_decreases_classification(
        self, d1: float, d2: float
    ) -> None:
        """For any d2 > d1, classification(d2) >= classification(d1).

        **Validates: Requirement 2.5**
        """
        assume(d2 > d1)

        classification_d1 = classify_deviation(d1)
        classification_d2 = classify_deviation(d2)

        severity_d1 = _SEVERITY_ORDER[classification_d1]
        severity_d2 = _SEVERITY_ORDER[classification_d2]

        assert severity_d2 >= severity_d1, (
            f"Monotonicity violated: classify_deviation({d2}) = {classification_d2.value} "
            f"(order {severity_d2}) < classify_deviation({d1}) = {classification_d1.value} "
            f"(order {severity_d1})"
        )

    @given(
        d1=st.floats(min_value=-1000.0, max_value=0.0, allow_nan=False, allow_infinity=False),
        d2=st.floats(min_value=-1000.0, max_value=0.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=500)
    def test_negative_deviations_monotonic_by_absolute_value(
        self, d1: float, d2: float
    ) -> None:
        """For negative deviations, monotonicity holds on absolute values.

        Since classify_deviation uses abs(deviation_factor), if |d2| > |d1|
        then classification(d2) >= classification(d1).

        **Validates: Requirement 2.5**
        """
        assume(abs(d2) > abs(d1))

        classification_d1 = classify_deviation(d1)
        classification_d2 = classify_deviation(d2)

        severity_d1 = _SEVERITY_ORDER[classification_d1]
        severity_d2 = _SEVERITY_ORDER[classification_d2]

        assert severity_d2 >= severity_d1, (
            f"Monotonicity violated for negative deviations: "
            f"classify_deviation({d2}) = {classification_d2.value} (order {severity_d2}) < "
            f"classify_deviation({d1}) = {classification_d1.value} (order {severity_d1}). "
            f"|d2|={abs(d2)}, |d1|={abs(d1)}"
        )

    @given(
        d1=st.floats(min_value=-1000.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
        d2=st.floats(min_value=-1000.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=500)
    def test_mixed_sign_deviations_monotonic_by_absolute_value(
        self, d1: float, d2: float
    ) -> None:
        """For any sign combination, monotonicity holds on absolute values.

        **Validates: Requirement 2.5**
        """
        assume(abs(d2) > abs(d1))

        classification_d1 = classify_deviation(d1)
        classification_d2 = classify_deviation(d2)

        severity_d1 = _SEVERITY_ORDER[classification_d1]
        severity_d2 = _SEVERITY_ORDER[classification_d2]

        assert severity_d2 >= severity_d1, (
            f"Monotonicity violated: classify_deviation({d2}) = {classification_d2.value} "
            f"(order {severity_d2}) < classify_deviation({d1}) = {classification_d1.value} "
            f"(order {severity_d1}). |d2|={abs(d2)}, |d1|={abs(d1)}"
        )
