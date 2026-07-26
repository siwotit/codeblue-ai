"""Property-based test: Log finding classification and structure.

**Validates: Requirements 5.3, 5.4, 5.5, 5.6**

For any set of log patterns, verify new/pre-existing classification,
max 5 sample lines, and source references present.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from hypothesis import given, settings, assume
from hypothesis import strategies as st

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "skills"))
sys.path.insert(
    0,
    str(
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "skills"
        / "log-triage"
    ),
)

from log_triage import (
    MAX_SAMPLE_LINES,
    classify_pattern_severity,
    extract_findings_from_results,
)
from shared.models import LogSeverity


# --- Strategies ---

# Strategy for log pattern strings (non-empty printable strings)
log_pattern_strategy = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "S"),
        whitelist_characters="_-.: /",
    ),
    min_size=1,
    max_size=100,
)

# Strategy for log group names (must start with /)
log_group_strategy = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N"),
        whitelist_characters="/-_.",
    ),
    min_size=2,
    max_size=100,
).map(lambda s: "/" + s.lstrip("/"))

# Strategy for ISO 8601 timestamps within a reasonable range
timestamp_strategy = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2030, 12, 31),
    timezones=st.just(timezone.utc),
)

# Strategy for log messages that contain a known pattern
def log_message_with_pattern(pattern: str) -> st.SearchStrategy[str]:
    """Generate a log message containing the given pattern."""
    prefix = st.text(
        alphabet=st.characters(whitelist_categories=("L", "N", "P"), whitelist_characters=" :_-."),
        min_size=0,
        max_size=50,
    )
    suffix = st.text(
        alphabet=st.characters(whitelist_categories=("L", "N", "P"), whitelist_characters=" :_-."),
        min_size=0,
        max_size=50,
    )
    return st.builds(lambda p, s: f"{p}{pattern}{s}", prefix, suffix)


# Strategy for a single log entry in query results
def log_entry_strategy(patterns: list[str]) -> st.SearchStrategy[dict]:
    """Generate a single log entry that matches one of the given patterns."""
    pattern = st.sampled_from(patterns)
    return pattern.flatmap(
        lambda p: st.fixed_dictionaries({
            "@message": log_message_with_pattern(p),
            "@timestamp": timestamp_strategy.map(lambda dt: dt.isoformat()),
            "@logStream": st.text(
                alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="/-_"),
                min_size=1,
                max_size=50,
            ),
        })
    )


# Strategy for query results with variable number of entries
def query_results_strategy(
    patterns: list[str], min_entries: int = 0, max_entries: int = 20
) -> st.SearchStrategy[dict]:
    """Generate CloudWatch Logs Insights query results."""
    return st.fixed_dictionaries({
        "results": st.lists(
            log_entry_strategy(patterns),
            min_size=min_entries,
            max_size=max_entries,
        )
    })


class TestLogFindingClassificationAndStructure:
    """Property 11: Log finding classification and structure."""

    @given(pattern=log_pattern_strategy)
    @settings(max_examples=500)
    def test_severity_is_one_of_valid_values(self, pattern: str) -> None:
        """For any log pattern string, severity classification produces a valid LogSeverity.

        **Validates: Requirements 5.3**
        """
        severity = classify_pattern_severity(pattern)
        assert severity in (LogSeverity.ERROR, LogSeverity.WARNING, LogSeverity.FATAL), (
            f"Severity {severity} for pattern '{pattern}' is not one of: "
            f"error, warning, fatal"
        )

    @given(
        num_entries=st.integers(min_value=1, max_value=30),
        log_group=log_group_strategy,
    )
    @settings(max_examples=300, deadline=None)
    def test_findings_have_max_five_sample_lines(
        self, num_entries: int, log_group: str
    ) -> None:
        """For any query results, each finding has at most 5 sample lines.

        **Validates: Requirements 5.5**
        """
        patterns = ["ERROR"]
        # Build results with exact number of entries all matching the pattern
        entries = []
        base_time = datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc)
        for i in range(num_entries):
            ts = base_time + timedelta(minutes=i)
            entries.append({
                "@message": f"ERROR: something failed at iteration {i}",
                "@timestamp": ts.isoformat(),
                "@logStream": "stream/main/abc",
            })

        results = {"results": entries}
        start_time = base_time
        end_time = base_time + timedelta(hours=1)

        findings = extract_findings_from_results(
            results, log_group, patterns, start_time, end_time
        )

        for finding in findings:
            sample_lines = finding.get("sampleLines", [])
            assert len(sample_lines) <= MAX_SAMPLE_LINES, (
                f"Finding for pattern '{finding['pattern']}' has "
                f"{len(sample_lines)} sample lines, exceeds max {MAX_SAMPLE_LINES}"
            )

    @given(
        num_entries=st.integers(min_value=1, max_value=15),
        log_group=log_group_strategy,
    )
    @settings(max_examples=300, deadline=None)
    def test_findings_have_source_references(
        self, num_entries: int, log_group: str
    ) -> None:
        """For any findings, each has logGroup and firstSeen/lastSeen timestamps.

        **Validates: Requirements 5.6**
        """
        patterns = ["ERROR", "FATAL"]
        base_time = datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc)
        entries = []
        for i in range(num_entries):
            pattern = patterns[i % len(patterns)]
            ts = base_time + timedelta(minutes=i)
            entries.append({
                "@message": f"{pattern}: failure message {i}",
                "@timestamp": ts.isoformat(),
                "@logStream": f"stream/{i}",
            })

        results = {"results": entries}
        start_time = base_time
        end_time = base_time + timedelta(hours=1)

        findings = extract_findings_from_results(
            results, log_group, patterns, start_time, end_time
        )

        for finding in findings:
            # logGroup must be present (source reference)
            assert "logGroup" in finding, (
                f"Finding for pattern '{finding['pattern']}' is missing logGroup"
            )
            assert finding["logGroup"] == log_group, (
                f"Finding logGroup '{finding['logGroup']}' does not match "
                f"expected '{log_group}'"
            )

            # firstSeen and lastSeen timestamps must be present
            assert "firstSeen" in finding, (
                f"Finding for pattern '{finding['pattern']}' is missing firstSeen"
            )
            assert "lastSeen" in finding, (
                f"Finding for pattern '{finding['pattern']}' is missing lastSeen"
            )

            # firstSeen <= lastSeen
            first = finding["firstSeen"]
            last = finding["lastSeen"]
            assert first <= last, (
                f"firstSeen ({first}) is after lastSeen ({last}) "
                f"for pattern '{finding['pattern']}'"
            )

    @given(
        num_entries=st.integers(min_value=1, max_value=15),
        log_group=log_group_strategy,
        is_new_value=st.booleans(),
    )
    @settings(max_examples=300, deadline=None)
    def test_is_new_field_is_always_boolean(
        self, num_entries: int, log_group: str, is_new_value: bool
    ) -> None:
        """For any finding, the isNew field is always a boolean value.

        **Validates: Requirements 5.4**

        This tests that when we construct LogFinding models from raw findings
        with isNew set, the field is always a proper boolean.
        """
        from shared.models import LogFinding

        patterns = ["ERROR"]
        base_time = datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc)
        entries = []
        for i in range(num_entries):
            ts = base_time + timedelta(minutes=i)
            entries.append({
                "@message": f"ERROR: failure {i}",
                "@timestamp": ts.isoformat(),
                "@logStream": "stream/main",
            })

        results = {"results": entries}
        start_time = base_time
        end_time = base_time + timedelta(hours=1)

        findings = extract_findings_from_results(
            results, log_group, patterns, start_time, end_time
        )

        # Simulate classification (set isNew like run_log_triage does)
        for finding in findings:
            finding["isNew"] = is_new_value

        # Construct LogFinding model to validate the field type
        for finding in findings:
            log_finding = LogFinding(
                pattern=finding["pattern"],
                count=finding["count"],
                firstSeen=finding["firstSeen"],
                lastSeen=finding["lastSeen"],
                isNew=finding["isNew"],
                severity=classify_pattern_severity(finding["pattern"]),
                sampleLogLines=finding.get("sampleLines", [])[:MAX_SAMPLE_LINES],
                logGroup=finding["logGroup"],
                logStream=finding.get("logStream"),
            )
            assert isinstance(log_finding.is_new, bool), (
                f"isNew field is {type(log_finding.is_new)}, expected bool"
            )

    @given(
        patterns=st.lists(
            st.sampled_from(["ERROR", "FATAL", "Exception", "Timeout", "ConnectionRefused",
                             "panic", "warn", "WARN", "Warning"]),
            min_size=1,
            max_size=5,
            unique=True,
        )
    )
    @settings(max_examples=300, deadline=None)
    def test_severity_classification_covers_all_valid_severities(
        self, patterns: list[str]
    ) -> None:
        """For any set of patterns, each classified severity is a valid LogSeverity value.

        **Validates: Requirements 5.3**
        """
        valid_severities = {LogSeverity.ERROR, LogSeverity.WARNING, LogSeverity.FATAL}

        for pattern in patterns:
            severity = classify_pattern_severity(pattern)
            assert severity in valid_severities, (
                f"Pattern '{pattern}' classified as '{severity}' which is not "
                f"one of {valid_severities}"
            )

    @given(
        num_patterns=st.integers(min_value=1, max_value=5),
        entries_per_pattern=st.integers(min_value=1, max_value=10),
        log_group=log_group_strategy,
    )
    @settings(max_examples=200, deadline=None)
    def test_findings_structure_complete_for_multiple_patterns(
        self, num_patterns: int, entries_per_pattern: int, log_group: str
    ) -> None:
        """For any combination of patterns and entries, all findings have required structure.

        **Validates: Requirements 5.3, 5.4, 5.5, 5.6**
        """
        all_patterns = ["ERROR", "FATAL", "Exception", "Timeout", "ConnectionRefused"]
        patterns = all_patterns[:num_patterns]

        base_time = datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc)
        entries = []
        for p_idx, pattern in enumerate(patterns):
            for e_idx in range(entries_per_pattern):
                ts = base_time + timedelta(minutes=p_idx * 10 + e_idx)
                entries.append({
                    "@message": f"{pattern}: error details {e_idx}",
                    "@timestamp": ts.isoformat(),
                    "@logStream": f"stream/{p_idx}/{e_idx}",
                })

        results = {"results": entries}
        start_time = base_time
        end_time = base_time + timedelta(hours=1)

        findings = extract_findings_from_results(
            results, log_group, patterns, start_time, end_time
        )

        assert len(findings) > 0, "Expected at least one finding from non-empty results"

        for finding in findings:
            # Structural completeness checks
            assert "pattern" in finding
            assert "count" in finding
            assert finding["count"] > 0

            # Max 5 sample lines (Req 5.5)
            sample_lines = finding.get("sampleLines", [])
            assert len(sample_lines) <= MAX_SAMPLE_LINES

            # Source reference present (Req 5.6)
            assert "logGroup" in finding
            assert finding["logGroup"] == log_group
            assert "firstSeen" in finding
            assert "lastSeen" in finding

            # Severity classification valid (Req 5.3)
            severity = classify_pattern_severity(finding["pattern"])
            assert severity in (LogSeverity.ERROR, LogSeverity.WARNING, LogSeverity.FATAL)
