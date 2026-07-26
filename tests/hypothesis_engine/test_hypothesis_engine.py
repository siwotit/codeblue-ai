"""Unit tests for hypothesis_engine.py.

Tests the minimal script functionality:
- Temporal overlap scoring
- Input structuring for LLM
- Output schema validation
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Add skills directory to path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills"))
sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "skills" / "hypothesis-engine")
)

from hypothesis_engine import (
    build_insufficient_data_response,
    compute_confidence_adjustment,
    compute_temporal_overlap_seconds,
    compute_temporal_scores,
    identify_unavailable_signals,
    run_hypothesis_engine,
    score_temporal_proximity,
    structure_context_for_llm,
    validate_hypotheses_output,
)


# --- Temporal Overlap Scoring Tests ---


class TestTemporalOverlapSeconds:
    """Tests for compute_temporal_overlap_seconds."""

    def test_same_time(self):
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        assert compute_temporal_overlap_seconds(ts, ts) == 0.0

    def test_change_before_anomaly(self):
        change = datetime(2024, 1, 15, 9, 55, 0, tzinfo=timezone.utc)
        anomaly = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        assert compute_temporal_overlap_seconds(change, anomaly) == 300.0

    def test_change_after_anomaly(self):
        change = datetime(2024, 1, 15, 10, 5, 0, tzinfo=timezone.utc)
        anomaly = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        assert compute_temporal_overlap_seconds(change, anomaly) == 300.0

    def test_large_gap(self):
        change = datetime(2024, 1, 14, 10, 0, 0, tzinfo=timezone.utc)
        anomaly = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        assert compute_temporal_overlap_seconds(change, anomaly) == 86400.0


class TestScoreTemporalProximity:
    """Tests for score_temporal_proximity."""

    def test_zero_seconds_gives_max_score(self):
        assert score_temporal_proximity(0) == 1.0

    def test_within_5_minutes(self):
        score = score_temporal_proximity(150)  # 2.5 minutes
        assert 0.9 <= score <= 1.0

    def test_at_5_minutes(self):
        score = score_temporal_proximity(300)
        assert score == pytest.approx(0.9, abs=0.01)

    def test_within_15_minutes(self):
        score = score_temporal_proximity(600)  # 10 minutes
        assert 0.7 <= score <= 0.9

    def test_within_1_hour(self):
        score = score_temporal_proximity(1800)  # 30 minutes
        assert 0.4 <= score <= 0.7

    def test_within_6_hours(self):
        score = score_temporal_proximity(10800)  # 3 hours
        assert 0.2 <= score <= 0.4

    def test_within_24_hours(self):
        score = score_temporal_proximity(43200)  # 12 hours
        assert 0.05 <= score <= 0.2

    def test_beyond_24_hours(self):
        score = score_temporal_proximity(100000)
        assert score == 0.01

    def test_monotonic_decrease(self):
        """Score should decrease monotonically as time increases."""
        times = [0, 60, 300, 600, 900, 1800, 3600, 10800, 21600, 43200, 86400]
        scores = [score_temporal_proximity(t) for t in times]
        for i in range(len(scores) - 1):
            assert scores[i] >= scores[i + 1], (
                f"Score at {times[i]}s ({scores[i]}) should be >= "
                f"score at {times[i+1]}s ({scores[i+1]})"
            )


class TestComputeTemporalScores:
    """Tests for compute_temporal_scores."""

    def test_none_correlated_changes(self):
        result = compute_temporal_scores(None, None, None)
        assert result == []

    def test_empty_changes_list(self):
        result = compute_temporal_scores({"changes": []}, None, None)
        assert result == []

    def test_no_anchor_point(self):
        changes = {"changes": [{"id": "c1", "timestamp": "2024-01-15T10:00:00Z"}]}
        result = compute_temporal_scores(changes, None, None)
        assert len(result) == 1
        assert result[0]["temporalOverlapScore"] == 0.0
        assert result[0]["secondsFromAnomaly"] is None

    def test_with_anomaly_start_time(self):
        anomaly = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        changes = {
            "changes": [
                {"id": "c1", "timestamp": "2024-01-15T09:57:00Z"},  # 3 min before
                {"id": "c2", "timestamp": "2024-01-15T08:00:00Z"},  # 2 hours before
            ]
        }
        result = compute_temporal_scores(changes, anomaly, None)
        assert len(result) == 2
        # First change is closer, should have higher score
        assert result[0]["temporalOverlapScore"] > result[1]["temporalOverlapScore"]
        assert result[0]["secondsFromAnomaly"] == 180.0

    def test_prefers_anomaly_over_fired_at(self):
        anomaly = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        fired_at = datetime(2024, 1, 15, 10, 5, 0, tzinfo=timezone.utc)
        changes = {
            "changes": [{"id": "c1", "timestamp": "2024-01-15T09:58:00Z"}]
        }
        result = compute_temporal_scores(changes, anomaly, fired_at)
        # Should use anomaly (120s gap), not fired_at (420s gap)
        assert result[0]["secondsFromAnomaly"] == 120.0

    def test_falls_back_to_fired_at(self):
        fired_at = datetime(2024, 1, 15, 10, 5, 0, tzinfo=timezone.utc)
        changes = {
            "changes": [{"id": "c1", "timestamp": "2024-01-15T10:03:00Z"}]
        }
        result = compute_temporal_scores(changes, None, fired_at)
        assert result[0]["secondsFromAnomaly"] == 120.0


# --- Input Structuring Tests ---


class TestIdentifyUnavailableSignals:
    """Tests for identify_unavailable_signals."""

    def test_all_present(self):
        context = {
            "alert": {},
            "metricDeviation": {},
            "correlatedChanges": {},
            "logFindings": {},
            "clusterHealth": {},
        }
        assert identify_unavailable_signals(context) == []

    def test_all_missing(self):
        context = {"alert": {}}
        result = identify_unavailable_signals(context)
        assert set(result) == {"metricDeviation", "correlatedChanges", "logFindings", "clusterHealth"}

    def test_some_missing(self):
        context = {
            "alert": {},
            "metricDeviation": {},
            "correlatedChanges": None,
            "logFindings": {},
            "clusterHealth": None,
        }
        result = identify_unavailable_signals(context)
        assert set(result) == {"correlatedChanges", "clusterHealth"}


class TestComputeConfidenceAdjustment:
    """Tests for compute_confidence_adjustment."""

    def test_no_missing(self):
        assert compute_confidence_adjustment([]) == 0.0

    def test_one_missing(self):
        assert compute_confidence_adjustment(["metricDeviation"]) == -0.2

    def test_all_missing(self):
        signals = ["metricDeviation", "correlatedChanges", "logFindings", "clusterHealth"]
        assert compute_confidence_adjustment(signals) == -0.8


class TestStructureContextForLlm:
    """Tests for structure_context_for_llm."""

    def test_minimal_context(self):
        context = {
            "alert": {"firedAt": "2024-01-15T10:00:00Z", "title": "Test alert"},
        }
        result = structure_context_for_llm(context)
        assert result["alert"] == context["alert"]
        assert result["analysisMetadata"]["availableSignalCount"] == 0
        assert len(result["analysisMetadata"]["unavailableSignals"]) == 4

    def test_full_context_with_changes(self):
        context = {
            "alert": {"firedAt": "2024-01-15T10:05:00Z"},
            "metricDeviation": {"anomalyStartTime": "2024-01-15T10:00:00Z"},
            "correlatedChanges": {
                "changes": [{"id": "c1", "timestamp": "2024-01-15T09:58:00Z"}],
                "lookbackWindow": "24h",
            },
            "logFindings": {"findings": []},
            "clusterHealth": {"overallHealth": "healthy"},
        }
        result = structure_context_for_llm(context)
        assert result["analysisMetadata"]["availableSignalCount"] == 4
        assert result["analysisMetadata"]["unavailableSignals"] == []
        assert result["analysisMetadata"]["anchorSource"] == "metricDeviation.anomalyStartTime"
        # Check scored changes were computed
        assert "scoredChanges" in result["correlatedChanges"]
        assert len(result["correlatedChanges"]["scoredChanges"]) == 1


# --- Output Validation Tests ---


class TestValidateHypothesesOutput:
    """Tests for validate_hypotheses_output."""

    def _valid_hypothesis(self, rank: int = 1, confidence: float = 0.8):
        return {
            "rank": rank,
            "title": f"Hypothesis {rank}",
            "description": "A valid hypothesis description",
            "confidence": confidence,
            "supportingEvidence": [
                {"claim": "Something happened", "source": "metric-baseline: CPU", "weight": 0.8}
            ],
            "contradictingEvidence": [
                {"claim": "No contradicting evidence found", "source": "hypothesis-engine", "weight": 0.0}
            ],
            "suggestedVerification": ["kubectl get pods -n affected"],
        }

    def test_valid_single_hypothesis(self):
        data = {"hypotheses": [self._valid_hypothesis()]}
        errors = validate_hypotheses_output(data)
        assert errors == []

    def test_valid_multiple_hypotheses(self):
        data = {
            "hypotheses": [
                self._valid_hypothesis(1, 0.9),
                self._valid_hypothesis(2, 0.7),
                self._valid_hypothesis(3, 0.5),
            ]
        }
        errors = validate_hypotheses_output(data)
        assert errors == []

    def test_missing_hypotheses_field(self):
        errors = validate_hypotheses_output({})
        assert len(errors) == 1
        assert "Missing 'hypotheses' field" in errors[0]

    def test_empty_hypotheses_list(self):
        errors = validate_hypotheses_output({"hypotheses": []})
        assert any("at least 1" in e for e in errors)

    def test_too_many_hypotheses(self):
        data = {"hypotheses": [self._valid_hypothesis(i, 1.0 - i * 0.05) for i in range(1, 12)]}
        errors = validate_hypotheses_output(data)
        assert any("at most 10" in e for e in errors)

    def test_duplicate_ranks(self):
        data = {
            "hypotheses": [
                self._valid_hypothesis(1, 0.9),
                self._valid_hypothesis(1, 0.7),  # duplicate rank
            ]
        }
        errors = validate_hypotheses_output(data)
        assert any("duplicate rank" in e for e in errors)

    def test_non_descending_confidence(self):
        data = {
            "hypotheses": [
                self._valid_hypothesis(1, 0.5),
                self._valid_hypothesis(2, 0.8),  # higher than previous
            ]
        }
        errors = validate_hypotheses_output(data)
        assert any("descending order" in e for e in errors)

    def test_confidence_out_of_range(self):
        h = self._valid_hypothesis(1, 1.5)
        errors = validate_hypotheses_output({"hypotheses": [h]})
        assert any("out of range" in e for e in errors)

    def test_missing_supporting_evidence(self):
        h = self._valid_hypothesis()
        h["supportingEvidence"] = []
        errors = validate_hypotheses_output({"hypotheses": [h]})
        assert any("supporting evidence" in e for e in errors)

    def test_missing_source_in_evidence(self):
        h = self._valid_hypothesis()
        h["supportingEvidence"] = [{"claim": "No source", "weight": 0.5}]
        errors = validate_hypotheses_output({"hypotheses": [h]})
        assert any("source" in e for e in errors)

    def test_missing_contradicting_evidence(self):
        h = self._valid_hypothesis()
        del h["contradictingEvidence"]
        errors = validate_hypotheses_output({"hypotheses": [h]})
        assert any("contradictingEvidence" in e for e in errors)

    def test_missing_verification_steps(self):
        h = self._valid_hypothesis()
        h["suggestedVerification"] = []
        errors = validate_hypotheses_output({"hypotheses": [h]})
        assert any("verification step" in e for e in errors)


# --- Integration Tests for run_hypothesis_engine ---


class TestRunHypothesisEngine:
    """Tests for the main run_hypothesis_engine function."""

    def test_missing_alert(self):
        result = run_hypothesis_engine({})
        assert result["status"] == "error"
        assert "alert" in result["message"]

    def test_all_signals_unavailable(self):
        context = {"alert": {"firedAt": "2024-01-15T10:00:00Z", "title": "Test"}}
        result = run_hypothesis_engine(context)
        assert result["status"] == "success"
        assert "Insufficient" in result["message"]
        # Should produce the fallback hypothesis
        data = result["data"]
        assert len(data["hypotheses"]) == 1
        assert data["hypotheses"][0]["confidence"] == 0.1
        assert data["hypotheses"][0]["title"] == "Insufficient data for diagnosis"

    def test_some_signals_available(self):
        context = {
            "alert": {"firedAt": "2024-01-15T10:00:00Z"},
            "metricDeviation": {"anomalyStartTime": "2024-01-15T09:55:00Z"},
            "correlatedChanges": None,
            "logFindings": {"findings": []},
            "clusterHealth": None,
        }
        result = run_hypothesis_engine(context)
        assert result["status"] == "success"
        data = result["data"]
        assert "structuredContext" in data
        assert set(data["unavailableSignals"]) == {"correlatedChanges", "clusterHealth"}
        assert data["confidenceAdjustment"] == -0.4

    def test_validates_good_hypotheses(self):
        context = {
            "alert": {"firedAt": "2024-01-15T10:00:00Z"},
            "metricDeviation": {"anomalyStartTime": "2024-01-15T09:55:00Z"},
            "logFindings": {"findings": []},
            "generatedHypotheses": {
                "hypotheses": [
                    {
                        "rank": 1,
                        "title": "Deployment caused regression",
                        "description": "Recent deployment introduced memory leak",
                        "confidence": 0.85,
                        "supportingEvidence": [
                            {"claim": "Memory spiked", "source": "metric-baseline: Memory", "weight": 0.9}
                        ],
                        "contradictingEvidence": [
                            {"claim": "No contradicting evidence found", "source": "hypothesis-engine", "weight": 0.0}
                        ],
                        "suggestedVerification": ["Check pod memory usage"],
                    }
                ]
            },
        }
        result = run_hypothesis_engine(context)
        assert result["status"] == "success"
        assert "1 ranked hypotheses" in result["message"]

    def test_rejects_invalid_hypotheses(self):
        context = {
            "alert": {"firedAt": "2024-01-15T10:00:00Z"},
            "metricDeviation": {},
            "generatedHypotheses": {
                "hypotheses": [
                    {
                        "rank": 1,
                        "title": "Bad hypothesis",
                        "description": "Missing required fields",
                        "confidence": 1.5,  # Invalid
                        "supportingEvidence": [],  # Empty
                        "contradictingEvidence": [],  # Empty
                        "suggestedVerification": [],  # Empty
                    }
                ]
            },
        }
        result = run_hypothesis_engine(context)
        assert result["status"] == "error"
        assert "validation failed" in result["message"]
