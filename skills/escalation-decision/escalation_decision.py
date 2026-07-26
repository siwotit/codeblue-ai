"""Escalation Decision Skill — determines urgency and escalation for incidents.

Accepts EscalationInput JSON on stdin. Applies a deterministic 5-step
escalation heuristic (severity baseline → blast radius → time-of-day →
service criticality → escalation flag) and returns an EscalationDecision
wrapped in a SkillResponse envelope.

Usage:
    echo '{"severity": "critical", ...}' | uv run escalation_decision.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_URGENCY_LEVELS = ("business_hours", "notify_channel", "page_now")

_SEVERITY_BASELINE: dict[str, str] = {
    "critical": "page_now",
    "high": "notify_channel",
    "medium": "business_hours",
    "low": "business_hours",
}

_BUSINESS_HOURS_START = 9   # inclusive
_BUSINESS_HOURS_END = 17    # exclusive


# ---------------------------------------------------------------------------
# Urgency helpers
# ---------------------------------------------------------------------------


def _urgency_index(urgency: str) -> int:
    """Return the numeric index of an urgency level (higher = more urgent)."""
    return _URGENCY_LEVELS.index(urgency)


def _increase_urgency(current: str) -> str:
    """Increase urgency by one level, capped at page_now."""
    idx = _urgency_index(current)
    new_idx = min(idx + 1, len(_URGENCY_LEVELS) - 1)
    return _URGENCY_LEVELS[new_idx]


# ---------------------------------------------------------------------------
# Time-of-day evaluation
# ---------------------------------------------------------------------------


def _is_outside_business_hours(current_time_utc: str, team_timezone: str) -> bool:
    """Determine if the current time is outside business hours (09:00-17:00)
    in the team's timezone.

    Returns True if outside business hours. If timezone conversion fails,
    assumes outside business hours (conservative escalation).
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        # Python < 3.9 fallback — assume outside hours
        return True

    try:
        # Parse the UTC time
        utc_time = datetime.fromisoformat(current_time_utc.replace("Z", "+00:00"))
        tz = ZoneInfo(team_timezone)
        local_time = utc_time.astimezone(tz)
        local_hour = local_time.hour
        return local_hour < _BUSINESS_HOURS_START or local_hour >= _BUSINESS_HOURS_END
    except Exception:
        # If timezone conversion fails, assume outside business hours
        return True


# ---------------------------------------------------------------------------
# Blast radius evaluation
# ---------------------------------------------------------------------------


def _is_multi_service(blast_radius: dict[str, Any] | None) -> bool:
    """Return True if blast radius spans multiple distinct services."""
    if not blast_radius:
        return False
    affected_services = blast_radius.get("affectedServices", [])
    return len(affected_services) > 1


# ---------------------------------------------------------------------------
# Service criticality evaluation
# ---------------------------------------------------------------------------


def _has_tier1_service(
    blast_radius: dict[str, Any] | None,
    service_criticality: dict[str, str] | None,
) -> tuple[bool, str | None]:
    """Check if any affected service is tier-1.

    Returns (is_tier1, tier1_service_name).
    """
    if not service_criticality or not blast_radius:
        return False, None

    affected_services = blast_radius.get("affectedServices", [])
    for svc in affected_services:
        tier = service_criticality.get(svc, "")
        if tier == "tier-1":
            return True, svc

    return False, None


# ---------------------------------------------------------------------------
# Suggested responders
# ---------------------------------------------------------------------------


def _suggest_responders(
    blast_radius: dict[str, Any] | None,
    service_criticality: dict[str, str] | None,
) -> list[str]:
    """Derive suggested responder teams from service criticality metadata.

    Returns at most 3 responder team names.
    """
    if not service_criticality or not blast_radius:
        return []

    affected_services = blast_radius.get("affectedServices", [])
    responders: list[str] = []
    for svc in affected_services:
        tier = service_criticality.get(svc, "")
        if tier == "tier-1":
            responders.append(f"{svc}-oncall")

    # If we have tier-1 responders, add platform engineering as support
    if responders:
        responders.append("platform-engineering")

    return responders[:3]


# ---------------------------------------------------------------------------
# Reason generation
# ---------------------------------------------------------------------------


def _generate_reason(
    severity: str,
    urgency: str,
    blast_radius: dict[str, Any] | None,
    multi_service: bool,
    outside_hours: bool,
    tier1_service: str | None,
    unavailable_sources: list[str],
    current_time_utc: str,
    team_timezone: str,
    baseline_urgency: str,
) -> str:
    """Generate a human-readable reason referencing severity and primary factor."""
    parts: list[str] = []

    # Determine the primary influencing factor
    # Priority: tier-1 override > blast radius bump > time-of-day bump > baseline only
    if tier1_service and severity in ("critical", "high"):
        parts.append(
            f"{severity.capitalize()} severity incident affecting tier-1 service "
            f"{tier1_service}. Critical service classification requires immediate paging."
        )
    elif multi_service and _urgency_index(urgency) > _urgency_index(baseline_urgency):
        affected_services = (blast_radius or {}).get("affectedServices", [])
        svc_count = len(affected_services)
        svc_list = ", ".join(affected_services[:5])
        parts.append(
            f"{severity.capitalize()} severity incident with blast radius spanning "
            f"{svc_count} services ({svc_list}). Multi-service impact increases urgency."
        )
    elif outside_hours and severity in ("critical", "high"):
        parts.append(
            f"{severity.capitalize()} severity incident occurring outside business hours "
            f"({team_timezone}). Off-hours timing increases urgency."
        )
    else:
        # Baseline severity alone determined urgency
        if blast_radius:
            affected_services = blast_radius.get("affectedServices", [])
            pod_count = blast_radius.get("impactedPodCount", 0)
            if affected_services:
                svc = affected_services[0]
                parts.append(
                    f"{severity.capitalize()} severity incident affecting {svc}"
                    + (f" ({pod_count} pods)." if pod_count else ".")
                )
            else:
                parts.append(
                    f"{severity.capitalize()} severity incident."
                )
        else:
            parts.append(f"{severity.capitalize()} severity incident.")

        if severity == "critical":
            parts.append("Critical severity warrants immediate paging.")
        elif severity in ("medium", "low"):
            parts.append("Impact is contained and can be addressed during business hours.")

    # Note incomplete data
    if unavailable_sources:
        sources_str = ", ".join(unavailable_sources)
        parts.append(f"Note: incomplete assessment (unavailable sources: {sources_str}).")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Core escalation logic
# ---------------------------------------------------------------------------


def make_escalation_decision(input_data: dict[str, Any]) -> dict[str, Any]:
    """Apply the 5-step escalation heuristic and return EscalationDecision.

    Steps:
      1. Severity → baseline urgency
      2. Blast radius escalation (multi-service → +1 level)
      3. Time-of-day escalation (off-hours + high+ severity → +1 level)
      4. Service criticality override (tier-1 + high+ → page_now)
      5. Set escalation flag based on final urgency
    """
    # --- Validate required fields ---
    severity = input_data.get("severity")
    if not severity:
        return _error("Escalation decision failed: severity field missing from input")

    severity = severity.lower()
    if severity not in _SEVERITY_BASELINE:
        return _error(
            f"Escalation decision failed: invalid severity '{severity}'. "
            f"Must be one of: critical, high, medium, low"
        )

    blast_radius = input_data.get("blastRadius")
    current_time_utc = input_data.get("currentTimeUtc", "")
    team_timezone = input_data.get("teamTimezone", "UTC")
    service_criticality = input_data.get("serviceCriticality")
    unavailable_sources = input_data.get("unavailableSources", []) or []

    # --- Step 1: Severity-to-urgency baseline ---
    urgency = _SEVERITY_BASELINE[severity]
    baseline_urgency = urgency

    # --- Step 2: Blast radius escalation ---
    multi_service = _is_multi_service(blast_radius)
    if multi_service:
        urgency = _increase_urgency(urgency)

    # --- Step 3: Time-of-day escalation ---
    outside_hours = False
    if current_time_utc and severity in ("critical", "high"):
        outside_hours = _is_outside_business_hours(current_time_utc, team_timezone)
        if outside_hours:
            urgency = _increase_urgency(urgency)

    # --- Step 4: Service criticality override ---
    tier1_found, tier1_service = _has_tier1_service(blast_radius, service_criticality)
    if tier1_found and severity in ("critical", "high"):
        urgency = "page_now"

    # --- Step 5: Escalation flag ---
    escalate = urgency != "business_hours"

    # --- Generate reason ---
    reason = _generate_reason(
        severity=severity,
        urgency=urgency,
        blast_radius=blast_radius,
        multi_service=multi_service,
        outside_hours=outside_hours,
        tier1_service=tier1_service if tier1_found else None,
        unavailable_sources=unavailable_sources,
        current_time_utc=current_time_utc,
        team_timezone=team_timezone,
        baseline_urgency=baseline_urgency,
    )

    # --- Suggested responders ---
    suggested_responders = _suggest_responders(blast_radius, service_criticality)

    # --- Build response ---
    decision = {
        "escalate": escalate,
        "reason": reason,
        "urgency": urgency,
        "suggestedResponders": suggested_responders,
    }

    return _success(
        data=decision,
        message=f"Escalation decision: {urgency} — {reason[:80]}",
    )


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _success(data: dict[str, Any], message: str = "") -> dict[str, Any]:
    """Wrap data in a success SkillResponse."""
    return {"status": "success", "message": message, "data": data}


def _error(message: str) -> dict[str, Any]:
    """Wrap message in an error SkillResponse."""
    return {"status": "error", "message": message, "data": None}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Read JSON from stdin, process, and write result to stdout."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            result = _error("Empty input received on stdin")
        else:
            input_data = json.loads(raw)
            result = make_escalation_decision(input_data)
    except json.JSONDecodeError as e:
        result = _error(f"Invalid JSON input: {e}")
    except Exception as e:
        result = _error(f"Unexpected error during escalation decision: {e}")

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
