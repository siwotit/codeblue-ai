"""Hypothesis Engine skill for CodeBlue AI.

Minimal script for the prompt-heavy hypothesis-engine skill. The LLM performs
the core hypothesis generation via SKILL.md reasoning heuristics. This script:
1. Computes temporal overlap scores (|change.timestamp - anomalyStartTime|)
2. Structures input data for LLM reasoning (formats signals into digestible context)
3. Validates output schema (1-10 hypotheses, unique ranks, descending confidence)

Input: IncidentContext JSON on stdin
Output: SkillResponse wrapping RankedHypotheses JSON on stdout

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

# Add parent paths for imports when running via uv
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from shared.models import (
    SkillResponse,
)


# --- Temporal Overlap Scoring (Requirement 6.1) ---


def compute_temporal_overlap_seconds(
    change_timestamp: datetime,
    anomaly_start_time: datetime,
) -> float:
    """Compute absolute time difference in seconds between a change and anomaly start.

    Args:
        change_timestamp: When the change occurred.
        anomaly_start_time: When the anomaly was first detected.

    Returns:
        Absolute difference in seconds.
    """
    delta = abs((change_timestamp - anomaly_start_time).total_seconds())
    return delta


def score_temporal_proximity(seconds_apart: float) -> float:
    """Score temporal proximity based on SKILL.md heuristics.

    Scoring rules from SKILL.md:
    - 0-5 minutes (0-300s): Very high (0.9-1.0)
    - 5-15 minutes (300-900s): High (0.7-0.9)
    - 15-60 minutes (900-3600s): Moderate (0.4-0.7)
    - 1-6 hours (3600-21600s): Low (0.2-0.4)
    - 6-24 hours (21600-86400s): Very low (0.05-0.2)
    - >24 hours: Minimal (0.01-0.05)

    Uses linear interpolation within each band.

    Args:
        seconds_apart: Absolute time difference in seconds.

    Returns:
        Temporal proximity score between 0.0 and 1.0.
    """
    if seconds_apart <= 300:
        # 0-5 min: linear from 1.0 to 0.9
        return 1.0 - (seconds_apart / 300) * 0.1
    elif seconds_apart <= 900:
        # 5-15 min: linear from 0.9 to 0.7
        return 0.9 - ((seconds_apart - 300) / 600) * 0.2
    elif seconds_apart <= 3600:
        # 15-60 min: linear from 0.7 to 0.4
        return 0.7 - ((seconds_apart - 900) / 2700) * 0.3
    elif seconds_apart <= 21600:
        # 1-6 hours: linear from 0.4 to 0.2
        return 0.4 - ((seconds_apart - 3600) / 18000) * 0.2
    elif seconds_apart <= 86400:
        # 6-24 hours: linear from 0.2 to 0.05
        return 0.2 - ((seconds_apart - 21600) / 64800) * 0.15
    else:
        # >24 hours: minimal
        return 0.01


def compute_temporal_scores(
    correlated_changes: dict[str, Any] | None,
    anomaly_start_time: datetime | None,
    alert_fired_at: datetime | None,
) -> list[dict[str, Any]]:
    """Compute temporal overlap scores for all correlated changes.

    Uses anomalyStartTime as anchor (preferred), falls back to alert.firedAt.

    Args:
        correlated_changes: The correlatedChanges data from IncidentContext.
        anomaly_start_time: When the metric anomaly started (preferred anchor).
        alert_fired_at: When the alert fired (fallback anchor).

    Returns:
        List of change dicts enriched with temporalOverlapScore and
        secondsFromAnomaly fields.
    """
    if not correlated_changes:
        return []

    changes = correlated_changes.get("changes", [])
    if not changes:
        return []

    # Determine anchor point (Requirement 6.1)
    anchor = anomaly_start_time or alert_fired_at
    if anchor is None:
        # No anchor available — can't score temporally
        return [
            {**change, "temporalOverlapScore": 0.0, "secondsFromAnomaly": None}
            for change in changes
        ]

    scored_changes = []
    for change in changes:
        change_ts_raw = change.get("timestamp")
        if change_ts_raw is None:
            scored_changes.append(
                {**change, "temporalOverlapScore": 0.0, "secondsFromAnomaly": None}
            )
            continue

        change_ts = _parse_timestamp(change_ts_raw)
        if change_ts is None:
            scored_changes.append(
                {**change, "temporalOverlapScore": 0.0, "secondsFromAnomaly": None}
            )
            continue

        seconds_apart = compute_temporal_overlap_seconds(change_ts, anchor)
        score = score_temporal_proximity(seconds_apart)

        scored_changes.append(
            {
                **change,
                "temporalOverlapScore": round(score, 4),
                "secondsFromAnomaly": round(seconds_apart, 1),
            }
        )

    return scored_changes


# --- Input Structuring for LLM ---


def identify_unavailable_signals(context: dict[str, Any]) -> list[str]:
    """Identify which signal sources are unavailable (null/missing).

    Args:
        context: The IncidentContext dictionary.

    Returns:
        List of unavailable signal type names.
    """
    unavailable = []
    signal_fields = ["metricDeviation", "correlatedChanges", "logFindings", "clusterHealth"]

    for field in signal_fields:
        if context.get(field) is None:
            unavailable.append(field)

    return unavailable


def compute_confidence_adjustment(unavailable_signals: list[str]) -> float:
    """Compute the total confidence adjustment for missing signals.

    Per SKILL.md: -0.2 per missing source.

    Args:
        unavailable_signals: List of unavailable signal type names.

    Returns:
        Negative float representing confidence reduction (e.g., -0.4 for 2 missing).
    """
    return -0.2 * len(unavailable_signals)


def structure_context_for_llm(context: dict[str, Any]) -> dict[str, Any]:
    """Structure the IncidentContext into a format digestible by the LLM.

    Enriches the context with:
    - Temporal overlap scores for correlated changes
    - Unavailable signal annotations
    - Confidence adjustment factor
    - Formatted summary of key signals

    Args:
        context: Raw IncidentContext dictionary.

    Returns:
        Enriched context dict ready for LLM reasoning.
    """
    alert = context.get("alert", {})
    metric_deviation = context.get("metricDeviation")
    correlated_changes = context.get("correlatedChanges")
    log_findings = context.get("logFindings")
    cluster_health = context.get("clusterHealth")

    # Determine anomaly start time
    anomaly_start_time = None
    if metric_deviation:
        ast_raw = metric_deviation.get("anomalyStartTime")
        if ast_raw:
            anomaly_start_time = _parse_timestamp(ast_raw)

    # Fallback to alert firedAt
    alert_fired_at = None
    fired_at_raw = alert.get("firedAt")
    if fired_at_raw:
        alert_fired_at = _parse_timestamp(fired_at_raw)

    # Compute temporal overlap scores (Requirement 6.1)
    scored_changes = compute_temporal_scores(
        correlated_changes, anomaly_start_time, alert_fired_at
    )

    # Identify unavailable signals
    unavailable_signals = identify_unavailable_signals(context)

    # Confidence adjustment
    confidence_adjustment = compute_confidence_adjustment(unavailable_signals)

    # Build structured context
    structured = {
        "alert": alert,
        "metricDeviation": metric_deviation,
        "correlatedChanges": {
            **(correlated_changes or {}),
            "scoredChanges": scored_changes,
        } if correlated_changes or scored_changes else None,
        "logFindings": log_findings,
        "clusterHealth": cluster_health,
        "analysisMetadata": {
            "anomalyAnchorTime": (
                anomaly_start_time.isoformat() if anomaly_start_time
                else (alert_fired_at.isoformat() if alert_fired_at else None)
            ),
            "anchorSource": (
                "metricDeviation.anomalyStartTime" if anomaly_start_time
                else "alert.firedAt"
            ),
            "unavailableSignals": unavailable_signals,
            "confidenceAdjustment": confidence_adjustment,
            "availableSignalCount": 4 - len(unavailable_signals),
        },
    }

    return structured


# --- Output Schema Validation ---


def validate_hypotheses_output(data: dict[str, Any]) -> list[str]:
    """Validate the RankedHypotheses output schema.

    Enforces:
    - 1-10 hypotheses (Requirement 6.8)
    - Unique ranks (Requirement 6.7)
    - Descending confidence order (Requirement 6.2)
    - Confidence values 0.0-1.0 (Requirement 6.3)
    - At least 1 supporting evidence item with source (Requirement 6.4)
    - Contradicting evidence addressed (Requirement 6.5)
    - At least 1 verification step (Requirement 6.6)

    Args:
        data: The RankedHypotheses data dictionary.

    Returns:
        List of validation error messages. Empty list means valid.
    """
    errors: list[str] = []

    hypotheses = data.get("hypotheses")
    if hypotheses is None:
        errors.append("Missing 'hypotheses' field")
        return errors

    if not isinstance(hypotheses, list):
        errors.append("'hypotheses' must be a list")
        return errors

    # Requirement 6.8: 1-10 hypotheses
    count = len(hypotheses)
    if count < 1:
        errors.append("Must have at least 1 hypothesis (Requirement 6.8)")
    if count > 10:
        errors.append(f"Must have at most 10 hypotheses, got {count} (Requirement 6.8)")

    ranks_seen: set[int] = set()
    prev_confidence: float | None = None

    for i, hypothesis in enumerate(hypotheses):
        prefix = f"hypotheses[{i}]"

        # Check required fields exist
        if "rank" not in hypothesis:
            errors.append(f"{prefix}: missing 'rank'")
            continue
        if "confidence" not in hypothesis:
            errors.append(f"{prefix}: missing 'confidence'")
            continue

        rank = hypothesis["rank"]
        confidence = hypothesis["confidence"]

        # Requirement 6.7: Unique ranks
        if not isinstance(rank, int):
            errors.append(f"{prefix}: rank must be an integer, got {type(rank).__name__}")
        elif rank in ranks_seen:
            errors.append(f"{prefix}: duplicate rank {rank} (Requirement 6.7)")
        else:
            ranks_seen.add(rank)

        # Requirement 6.3: Confidence 0.0-1.0
        if not isinstance(confidence, (int, float)):
            errors.append(f"{prefix}: confidence must be numeric, got {type(confidence).__name__}")
        elif confidence < 0.0 or confidence > 1.0:
            errors.append(
                f"{prefix}: confidence {confidence} out of range [0.0, 1.0] (Requirement 6.3)"
            )

        # Requirement 6.2: Descending confidence order
        if prev_confidence is not None and isinstance(confidence, (int, float)):
            if confidence > prev_confidence:
                errors.append(
                    f"{prefix}: confidence {confidence} > previous {prev_confidence}, "
                    f"must be in descending order (Requirement 6.2)"
                )
        if isinstance(confidence, (int, float)):
            prev_confidence = confidence

        # Requirement 6.4: At least 1 supporting evidence with source
        supporting = hypothesis.get("supportingEvidence", [])
        if not isinstance(supporting, list) or len(supporting) < 1:
            errors.append(
                f"{prefix}: must have at least 1 supporting evidence item (Requirement 6.4)"
            )
        else:
            for j, evidence in enumerate(supporting):
                if not evidence.get("source"):
                    errors.append(
                        f"{prefix}.supportingEvidence[{j}]: missing 'source' reference "
                        f"(Requirement 6.4)"
                    )

        # Requirement 6.5: Contradicting evidence addressed
        contradicting = hypothesis.get("contradictingEvidence")
        if contradicting is None:
            errors.append(
                f"{prefix}: must include 'contradictingEvidence' field (Requirement 6.5)"
            )
        elif not isinstance(contradicting, list) or len(contradicting) < 1:
            errors.append(
                f"{prefix}: must have at least 1 contradicting evidence item or explicit "
                f"'No contradicting evidence found' entry (Requirement 6.5)"
            )

        # Requirement 6.6: At least 1 verification step
        verification = hypothesis.get("suggestedVerification", [])
        if not isinstance(verification, list) or len(verification) < 1:
            errors.append(
                f"{prefix}: must have at least 1 suggested verification step (Requirement 6.6)"
            )

        # Check title and description exist
        if not hypothesis.get("title"):
            errors.append(f"{prefix}: missing or empty 'title'")
        if not hypothesis.get("description"):
            errors.append(f"{prefix}: missing or empty 'description'")

    return errors


# --- Insufficient Data Check (Requirement 6.9) ---


def build_insufficient_data_response(
    unavailable_signals: list[str],
    confidence_adjustment: float,
) -> dict[str, Any]:
    """Build the "Insufficient data" fallback response.

    When no hypothesis can exceed 0.2 confidence, produce a single hypothesis
    at confidence 0.1 per Requirement 6.9.

    Args:
        unavailable_signals: List of signal types that were unavailable.
        confidence_adjustment: Total confidence reduction.

    Returns:
        RankedHypotheses dictionary.
    """
    return {
        "hypotheses": [
            {
                "rank": 1,
                "title": "Insufficient data for diagnosis",
                "description": (
                    f"Unable to generate a confident hypothesis. "
                    f"Missing signal sources: {', '.join(unavailable_signals) or 'none'}. "
                    f"Only the original alert is available for analysis."
                ),
                "confidence": 0.1,
                "supportingEvidence": [
                    {
                        "claim": "Alert fired but insufficient correlated signals available",
                        "source": "hypothesis-engine: signal availability check",
                        "timestamp": None,
                        "weight": 0.1,
                    }
                ],
                "contradictingEvidence": [
                    {
                        "claim": "No contradicting evidence found",
                        "source": "hypothesis-engine: exhaustive signal review",
                        "timestamp": None,
                        "weight": 0.0,
                    }
                ],
                "suggestedVerification": [
                    "Manually check signal sources that were unavailable: "
                    + ", ".join(unavailable_signals),
                ],
            }
        ],
        "unavailableSignals": unavailable_signals,
        "confidenceAdjustment": confidence_adjustment,
    }


# --- Utility ---


def _parse_timestamp(ts_raw: Any) -> datetime | None:
    """Parse a timestamp from various formats.

    Args:
        ts_raw: ISO 8601 string, Unix timestamp (int/float), or datetime.

    Returns:
        Timezone-aware datetime, or None if parsing fails.
    """
    if ts_raw is None:
        return None

    if isinstance(ts_raw, datetime):
        if ts_raw.tzinfo is None:
            return ts_raw.replace(tzinfo=timezone.utc)
        return ts_raw

    if isinstance(ts_raw, (int, float)):
        return datetime.fromtimestamp(ts_raw, tz=timezone.utc)

    if isinstance(ts_raw, str):
        try:
            # Handle Z suffix
            ts_str = ts_raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return None

    return None


# --- Main Skill Logic ---


def run_hypothesis_engine(context: dict[str, Any]) -> dict[str, Any]:
    """Execute the hypothesis engine skill.

    This minimal script:
    1. Computes temporal overlap scores for correlated changes
    2. Structures context for LLM reasoning
    3. Validates LLM-generated output (if provided in passthrough mode)

    In normal operation, this script returns the structured context and
    the LLM (via SKILL.md) performs the actual hypothesis generation.
    The script then validates the output.

    Args:
        context: IncidentContext dictionary with keys:
            - alert (required): NormalizedAlert
            - metricDeviation (optional): MetricDeviation
            - correlatedChanges (optional): CorrelatedChanges
            - logFindings (optional): LogFindings
            - clusterHealth (optional): ClusterHealthReport
            - generatedHypotheses (optional): Pre-generated hypotheses for validation

    Returns:
        SkillResponse dictionary.
    """
    # Validate alert is present (required)
    alert = context.get("alert")
    if not alert:
        return SkillResponse(
            status="error",
            message="Missing required 'alert' field in IncidentContext",
            data=None,
        ).model_dump(by_alias=True)

    # Structure context for LLM
    structured_context = structure_context_for_llm(context)
    unavailable_signals = structured_context["analysisMetadata"]["unavailableSignals"]
    confidence_adjustment = structured_context["analysisMetadata"]["confidenceAdjustment"]

    # Check if all signals are unavailable (only alert present) → Requirement 6.9
    available_signal_count = structured_context["analysisMetadata"]["availableSignalCount"]
    if available_signal_count == 0:
        # All signals are unavailable — produce insufficient data response
        data = build_insufficient_data_response(unavailable_signals, confidence_adjustment)
        return SkillResponse(
            status="success",
            message="Insufficient signals for confident diagnosis",
            data=data,
        ).model_dump(by_alias=True)

    # Check if pre-generated hypotheses are provided for validation
    generated_hypotheses = context.get("generatedHypotheses")
    if generated_hypotheses is not None:
        # Validate the LLM-generated output
        validation_errors = validate_hypotheses_output(generated_hypotheses)
        if validation_errors:
            return SkillResponse(
                status="error",
                message=(
                    f"Hypothesis output validation failed with {len(validation_errors)} error(s): "
                    + "; ".join(validation_errors[:5])
                ),
                data={
                    "validationErrors": validation_errors,
                    "structuredContext": structured_context,
                },
            ).model_dump(by_alias=True)

        # Enrich validated output with unavailable signals and confidence adjustment
        enriched_output = {
            **generated_hypotheses,
            "unavailableSignals": unavailable_signals,
            "confidenceAdjustment": confidence_adjustment,
        }

        hypotheses = generated_hypotheses.get("hypotheses", [])
        return SkillResponse(
            status="success",
            message=f"Generated {len(hypotheses)} ranked hypotheses for incident",
            data=enriched_output,
        ).model_dump(by_alias=True)

    # No pre-generated hypotheses: return structured context for LLM reasoning
    # The LLM will use this context + SKILL.md heuristics to generate hypotheses
    return SkillResponse(
        status="success",
        message="Structured context prepared for hypothesis generation",
        data={
            "structuredContext": structured_context,
            "unavailableSignals": unavailable_signals,
            "confidenceAdjustment": confidence_adjustment,
        },
    ).model_dump(by_alias=True)


def main() -> None:
    """Entry point: read IncidentContext from stdin, write SkillResponse to stdout."""
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            result = SkillResponse(
                status="error",
                message="No input provided on stdin",
                data=None,
            ).model_dump(by_alias=True)
        else:
            context = json.loads(raw_input)
            result = run_hypothesis_engine(context)
    except json.JSONDecodeError as e:
        result = SkillResponse(
            status="error",
            message=f"Invalid JSON input: {e}",
            data=None,
        ).model_dump(by_alias=True)
    except Exception as e:
        result = SkillResponse(
            status="error",
            message=f"Unexpected error: {type(e).__name__}: {e}",
            data=None,
        ).model_dump(by_alias=True)

    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
