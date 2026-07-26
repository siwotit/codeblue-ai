"""Unit tests for the Log Triage skill.

Tests cover input validation, finding classification, severity assignment,
query construction, and the overall run_log_triage function.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "skills"))
sys.path.insert(
    0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "skills" / "log-triage")
)

from log_triage import (
    DEFAULT_ERROR_PATTERNS,
    MAX_SAMPLE_LINES,
    build_count_query,
    build_insights_query,
    classify_pattern_severity,
    extract_findings_from_results,
    run_log_triage,
)
from shared.models import LogSeverity


# --- classify_pattern_severity tests ---


class TestClassifyPatternSeverity:
    def test_fatal_pattern(self):
        assert classify_pattern_severity("FATAL") == LogSeverity.FATAL

    def test_panic_pattern(self):
        assert classify_pattern_severity("panic: runtime error") == LogSeverity.FATAL

    def test_warning_pattern(self):
        assert classify_pattern_severity("WARN: retrying connection") == LogSeverity.WARNING

    def test_error_pattern(self):
        assert classify_pattern_severity("ERROR") == LogSeverity.ERROR

    def test_exception_pattern(self):
        assert classify_pattern_severity("Exception") == LogSeverity.ERROR

    def test_timeout_pattern(self):
        assert classify_pattern_severity("Timeout") == LogSeverity.ERROR

    def test_connection_refused_pattern(self):
        assert classify_pattern_severity("ConnectionRefused") == LogSeverity.ERROR


# --- build_insights_query tests ---


class TestBuildInsightsQuery:
    def test_default_patterns_query(self):
        query = build_insights_query(DEFAULT_ERROR_PATTERNS)
        assert "fields @timestamp, @message, @logStream" in query
        assert "filter" in query
        assert "ERROR" in query
        assert "FATAL" in query
        assert "Exception" in query
        assert "Timeout" in query
        assert "ConnectionRefused" in query
        assert "limit 1000" in query

    def test_custom_patterns_query(self):
        query = build_insights_query(["CustomError", "SpecificException"])
        assert "CustomError" in query
        assert "SpecificException" in query

    def test_single_pattern(self):
        query = build_insights_query(["ERROR"])
        assert "@message like /ERROR/" in query


# --- build_count_query tests ---


class TestBuildCountQuery:
    def test_count_query_structure(self):
        query = build_count_query(["ERROR", "FATAL"])
        assert "stats count(*) as errorCount" in query
        assert "ERROR" in query
        assert "FATAL" in query


# --- extract_findings_from_results tests ---


class TestExtractFindingsFromResults:
    def test_empty_results(self):
        results = {"results": []}
        findings = extract_findings_from_results(
            results, "/aws/ecs/test", DEFAULT_ERROR_PATTERNS,
            datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc),
            datetime(2024, 1, 15, 11, 0, tzinfo=timezone.utc),
        )
        assert findings == []

    def test_single_pattern_match(self):
        results = {
            "results": [
                {
                    "@message": "2024-01-15T10:23:14Z ERROR ConnectionRefused: db:5432",
                    "@timestamp": "2024-01-15T10:23:14+00:00",
                    "@logStream": "service/main/abc123",
                },
                {
                    "@message": "2024-01-15T10:24:01Z ERROR ConnectionRefused: db:5432",
                    "@timestamp": "2024-01-15T10:24:01+00:00",
                    "@logStream": "service/main/abc123",
                },
            ]
        }
        findings = extract_findings_from_results(
            results, "/aws/ecs/prod-payments", DEFAULT_ERROR_PATTERNS,
            datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc),
            datetime(2024, 1, 15, 11, 0, tzinfo=timezone.utc),
        )
        # Both lines match "ERROR" pattern
        assert len(findings) >= 1
        error_finding = next(f for f in findings if f["pattern"] == "ERROR")
        assert error_finding["count"] == 2
        assert error_finding["logGroup"] == "/aws/ecs/prod-payments"
        assert error_finding["logStream"] == "service/main/abc123"

    def test_max_sample_lines_respected(self):
        results = {
            "results": [
                {
                    "@message": f"ERROR line {i}",
                    "@timestamp": f"2024-01-15T10:{i:02d}:00+00:00",
                    "@logStream": "stream1",
                }
                for i in range(10)
            ]
        }
        findings = extract_findings_from_results(
            results, "/aws/ecs/test", ["ERROR"],
            datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc),
            datetime(2024, 1, 15, 11, 0, tzinfo=timezone.utc),
        )
        assert len(findings) == 1
        assert len(findings[0]["sampleLines"]) <= MAX_SAMPLE_LINES

    def test_multiple_patterns_grouped(self):
        results = {
            "results": [
                {
                    "@message": "FATAL: process crashed",
                    "@timestamp": "2024-01-15T10:05:00+00:00",
                    "@logStream": "stream1",
                },
                {
                    "@message": "Timeout: upstream took too long",
                    "@timestamp": "2024-01-15T10:06:00+00:00",
                    "@logStream": "stream1",
                },
                {
                    "@message": "FATAL: out of memory",
                    "@timestamp": "2024-01-15T10:07:00+00:00",
                    "@logStream": "stream1",
                },
            ]
        }
        findings = extract_findings_from_results(
            results, "/aws/ecs/test", ["FATAL", "Timeout"],
            datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc),
            datetime(2024, 1, 15, 11, 0, tzinfo=timezone.utc),
        )
        # Should have separate groups for FATAL and Timeout
        assert len(findings) == 2
        fatal_finding = next(f for f in findings if f["pattern"] == "FATAL")
        timeout_finding = next(f for f in findings if f["pattern"] == "Timeout")
        assert fatal_finding["count"] == 2
        assert timeout_finding["count"] == 1


# --- run_log_triage input validation tests ---


class TestRunLogTriageValidation:
    def test_missing_log_groups(self):
        result = run_log_triage({"timeRange": {"start": "2024-01-15T10:00:00Z", "end": "2024-01-15T11:00:00Z"}})
        assert result["status"] == "error"
        assert "logGroups" in result["message"]

    def test_missing_time_range(self):
        result = run_log_triage({"logGroups": ["/aws/ecs/test"]})
        assert result["status"] == "error"
        assert "timeRange" in result["message"]

    def test_empty_log_groups(self):
        result = run_log_triage({
            "logGroups": [],
            "timeRange": {"start": "2024-01-15T10:00:00Z", "end": "2024-01-15T11:00:00Z"},
        })
        assert result["status"] == "error"
        assert "non-empty" in result["message"]

    def test_invalid_log_group_name(self):
        result = run_log_triage({
            "logGroups": ["no-leading-slash"],
            "timeRange": {"start": "2024-01-15T10:00:00Z", "end": "2024-01-15T11:00:00Z"},
        })
        assert result["status"] == "error"
        assert "Invalid log group" in result["message"]

    def test_invalid_time_range(self):
        result = run_log_triage({
            "logGroups": ["/aws/ecs/test"],
            "timeRange": {"start": "not-a-date", "end": "2024-01-15T11:00:00Z"},
        })
        assert result["status"] == "error"
        assert "timeRange" in result["message"]


# --- run_log_triage with mocked MCP tests ---


class TestRunLogTriageWithMCP:
    @patch("log_triage.cloudwatch_mcp")
    def test_all_groups_inaccessible_returns_error(self, mock_cw):
        """When all log groups are inaccessible, return error (Req 5.7)."""
        mock_cw.invoke.return_value = MagicMock(success=False, error="AccessDenied")

        result = run_log_triage({
            "logGroups": ["/aws/ecs/test1", "/aws/ecs/test2"],
            "timeRange": {"start": "2024-01-15T10:00:00Z", "end": "2024-01-15T11:00:00Z"},
        })

        assert result["status"] == "error"
        assert "inaccessible" in result["message"]

    @patch("log_triage.cloudwatch_mcp")
    def test_successful_triage_with_findings(self, mock_cw):
        """Successful triage returns findings with correct structure."""
        # Mock the query responses
        def invoke_side_effect(tool_name, args):
            if "queryLogInsights" == tool_name:
                log_group = args.get("logGroupName", "")
                query = args.get("query", "")
                if "stats count" in query or "patternCount" in query:
                    # Baseline count query
                    return MagicMock(
                        success=True,
                        data={"results": [{"errorCount": "5", "patternCount": "0"}]}
                    )
                else:
                    # Current window query
                    return MagicMock(
                        success=True,
                        data={
                            "results": [
                                {
                                    "@message": "ERROR: connection timeout",
                                    "@timestamp": "2024-01-15T10:30:00+00:00",
                                    "@logStream": "ecs/service/abc",
                                }
                            ]
                        }
                    )
            return MagicMock(success=False)

        mock_cw.invoke.side_effect = invoke_side_effect

        result = run_log_triage({
            "logGroups": ["/aws/ecs/prod-service"],
            "timeRange": {"start": "2024-01-15T10:00:00Z", "end": "2024-01-15T11:00:00Z"},
        })

        assert result["status"] == "success"
        assert result["data"] is not None
        data = result["data"]
        assert "findings" in data
        assert "queriedLogGroups" in data
        assert "/aws/ecs/prod-service" in data["queriedLogGroups"]
        assert "timeRange" in data
        assert "totalErrorCount" in data
        assert "baselineErrorCount" in data

    @patch("log_triage.cloudwatch_mcp")
    def test_partial_success_with_some_inaccessible(self, mock_cw):
        """Partial success: some groups accessible, some not (Req 5.7)."""
        call_count = [0]

        def invoke_side_effect(tool_name, args):
            call_count[0] += 1
            log_group = args.get("logGroupName", "")
            if log_group == "/aws/ecs/inaccessible":
                return MagicMock(success=False, error="AccessDenied")
            query = args.get("query", "")
            if "stats count" in query or "patternCount" in query:
                return MagicMock(
                    success=True,
                    data={"results": [{"errorCount": "3", "patternCount": "2"}]}
                )
            return MagicMock(
                success=True,
                data={"results": []}
            )

        mock_cw.invoke.side_effect = invoke_side_effect

        result = run_log_triage({
            "logGroups": ["/aws/ecs/accessible", "/aws/ecs/inaccessible"],
            "timeRange": {"start": "2024-01-15T10:00:00Z", "end": "2024-01-15T11:00:00Z"},
        })

        assert result["status"] == "success"
        assert "inaccessible" in result["message"]
        assert "/aws/ecs/accessible" in result["data"]["queriedLogGroups"]
        assert "/aws/ecs/inaccessible" not in result["data"]["queriedLogGroups"]

    @patch("log_triage.cloudwatch_mcp")
    def test_empty_results_is_success(self, mock_cw):
        """Empty results (no errors found) is a valid success response."""
        def invoke_side_effect(tool_name, args):
            query = args.get("query", "")
            if "stats count" in query or "patternCount" in query:
                return MagicMock(
                    success=True,
                    data={"results": [{"errorCount": "0"}]}
                )
            return MagicMock(success=True, data={"results": []})

        mock_cw.invoke.side_effect = invoke_side_effect

        result = run_log_triage({
            "logGroups": ["/aws/ecs/prod-service"],
            "timeRange": {"start": "2024-01-15T10:00:00Z", "end": "2024-01-15T11:00:00Z"},
        })

        assert result["status"] == "success"
        assert result["data"]["totalErrorCount"] == 0
        assert result["data"]["findings"] == []

    @patch("log_triage.cloudwatch_mcp")
    def test_finding_sample_lines_capped_at_five(self, mock_cw):
        """Requirement 5.5: Max 5 representative log lines per finding."""
        def invoke_side_effect(tool_name, args):
            query = args.get("query", "")
            if "stats count" in query or "patternCount" in query:
                return MagicMock(
                    success=True,
                    data={"results": [{"errorCount": "0", "patternCount": "0"}]}
                )
            return MagicMock(
                success=True,
                data={
                    "results": [
                        {
                            "@message": f"ERROR line {i}",
                            "@timestamp": f"2024-01-15T10:{i:02d}:00+00:00",
                            "@logStream": "stream",
                        }
                        for i in range(10)
                    ]
                }
            )

        mock_cw.invoke.side_effect = invoke_side_effect

        result = run_log_triage({
            "logGroups": ["/aws/ecs/test"],
            "timeRange": {"start": "2024-01-15T10:00:00Z", "end": "2024-01-15T11:00:00Z"},
        })

        assert result["status"] == "success"
        for finding in result["data"]["findings"]:
            assert len(finding["sampleLogLines"]) <= MAX_SAMPLE_LINES

    @patch("log_triage.cloudwatch_mcp")
    def test_finding_includes_source_references(self, mock_cw):
        """Requirement 5.6: Each finding includes log group, timestamp, log stream."""
        def invoke_side_effect(tool_name, args):
            query = args.get("query", "")
            if "stats count" in query or "patternCount" in query:
                return MagicMock(
                    success=True,
                    data={"results": [{"errorCount": "0", "patternCount": "0"}]}
                )
            return MagicMock(
                success=True,
                data={
                    "results": [
                        {
                            "@message": "ERROR: timeout reached",
                            "@timestamp": "2024-01-15T10:30:00+00:00",
                            "@logStream": "service/main/xyz",
                        }
                    ]
                }
            )

        mock_cw.invoke.side_effect = invoke_side_effect

        result = run_log_triage({
            "logGroups": ["/aws/ecs/test"],
            "timeRange": {"start": "2024-01-15T10:00:00Z", "end": "2024-01-15T11:00:00Z"},
        })

        assert result["status"] == "success"
        findings = result["data"]["findings"]
        assert len(findings) > 0
        finding = findings[0]
        assert "logGroup" in finding
        assert finding["logGroup"] == "/aws/ecs/test"
        assert "firstSeen" in finding
        assert "lastSeen" in finding
        assert "logStream" in finding


# --- main() entry point tests ---


class TestMain:
    @patch("log_triage.cloudwatch_mcp")
    def test_main_reads_stdin_writes_stdout(self, mock_cw, capsys):
        """Main function reads JSON from stdin and writes response to stdout."""
        mock_cw.invoke.return_value = MagicMock(success=False, error="unavailable")

        input_data = json.dumps({
            "logGroups": ["/aws/ecs/test"],
            "timeRange": {"start": "2024-01-15T10:00:00Z", "end": "2024-01-15T11:00:00Z"},
        })

        from log_triage import main
        import io

        with patch("sys.stdin", io.StringIO(input_data)):
            main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "status" in output

    def test_main_empty_stdin(self, capsys):
        """Main function handles empty stdin gracefully."""
        from log_triage import main
        import io

        with patch("sys.stdin", io.StringIO("")):
            main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["status"] == "error"
        assert "No input" in output["message"]

    def test_main_invalid_json(self, capsys):
        """Main function handles invalid JSON gracefully."""
        from log_triage import main
        import io

        with patch("sys.stdin", io.StringIO("not json {")):
            main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["status"] == "error"
        assert "Invalid JSON" in output["message"]
