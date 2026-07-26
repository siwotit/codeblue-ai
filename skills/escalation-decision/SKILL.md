# Escalation Decision Skill

**Classification**: prompt-heavy

The SKILL.md contains the primary decision logic — escalation heuristics, threshold definitions, time-of-day rules, service criticality evaluation, and reasoning guidance for urgency classification. The script (`escalation_decision.py`) is minimal: it maps severity to a baseline urgency, checks time-of-day, evaluates blast radius scope, and returns a structured decision. The LLM synthesizes the final reason and applies judgment when multiple escalation factors interact.

---

## When to Invoke (Trigger Conditions)

Invoke this skill when **all** of the following conditions are met:

1. **Hypothesis generation is complete** — The hypothesis-engine skill has produced ranked hypotheses (or an "Insufficient data" hypothesis at minimum). The escalation decision uses the overall incident severity and blast radius, not the hypotheses directly.
2. **An IncidentReport is being assembled** — The orchestrator has severity assessment and blast radius data available, either from the alert itself or computed during signal collection.
3. **The orchestrator is in the `reporting` phase** — The workflow has transitioned past the hypothesis phase and is assembling the final report before formatting for Slack.

**Ordering**: This skill runs **after** hypothesis generation and **before** incident-summary-format. The escalation decision is included in the formatted Slack message.

**Skip conditions**: Never skip this skill. Every incident report must include an escalation decision, even when upstream data is incomplete.

---

## Input Contract

The script accepts an `EscalationInput` JSON object derived from the in-progress `IncidentReport`:

```json
{
  "severity": "critical",
  "blastRadius": {
    "summary": "12 pods across 3 nodes in us-east-1",
    "affectedServices": ["payments-service", "checkout-service", "inventory-service"],
    "affectedNamespaces": ["payments", "checkout"],
    "affectedNodes": ["ip-10-0-1-42", "ip-10-0-2-17", "ip-10-0-3-8"],
    "impactedPodCount": 12,
    "region": "us-east-1"
  },
  "currentTimeUtc": "2024-01-15T03:42:00Z",
  "teamTimezone": "America/New_York",
  "serviceCriticality": {
    "payments-service": "tier-1",
    "checkout-service": "tier-1",
    "inventory-service": "tier-2"
  },
  "unavailableSources": ["metricDeviation"],
  "confidence": 0.72
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `severity` | string | yes | Incident severity: one of `critical`, `high`, `medium`, `low` |
| `blastRadius` | BlastRadius | yes | Scope of impact including affected services, namespaces, nodes, pod count |
| `currentTimeUtc` | ISO8601 | yes | Current time in UTC for time-of-day evaluation |
| `teamTimezone` | string | yes | IANA timezone of the on-call team (e.g., `America/New_York`) for business hours calculation |
| `serviceCriticality` | Record<string, string> \| null | no | Map of service names to criticality tiers. Null if metadata unavailable. |
| `unavailableSources` | string[] | no | List of data sources that failed upstream. Empty if all sources succeeded. |
| `confidence` | float | no | Overall confidence in the incident assessment (0.0–1.0). Used for context in reason generation. |

### Constructing Input from Orchestrator State

- `severity` → `incidentReport.severity`
- `blastRadius` → `incidentReport.blastRadius`
- `currentTimeUtc` → Current UTC time at escalation evaluation
- `teamTimezone` → From CodeBlue AI configuration (team settings)
- `serviceCriticality` → From service catalog configuration (may be null if not configured)
- `unavailableSources` → Names of skills/MCPs that failed during this incident workflow
- `confidence` → `incidentReport.confidence`

---

## Output Contract

The script returns an `EscalationDecision` JSON object wrapped in a `SkillResponse` envelope:

```json
{
  "status": "success",
  "message": "Escalation decision: page_now — critical severity with multi-service blast radius",
  "data": {
    "escalate": true,
    "reason": "Critical severity incident affecting 3 services (payments-service, checkout-service, inventory-service) across 12 pods. Blast radius spans multiple services, increasing urgency. Tier-1 services affected with high+ severity requires immediate paging.",
    "urgency": "page_now",
    "suggestedResponders": ["payments-oncall", "platform-engineering"]
  }
}
```

### EscalationDecision Schema

| Field | Type | Description |
|-------|------|-------------|
| `escalate` | boolean | Whether escalation is recommended. `true` for `page_now` and `notify_channel`; `false` for `business_hours`. |
| `reason` | string | Non-empty human-readable rationale referencing severity and the primary influencing factor (blast radius, time-of-day, or service criticality). |
| `urgency` | string | One of: `page_now`, `notify_channel`, `business_hours` |
| `suggestedResponders` | string[] | Team or role names that should be notified. May be empty if responder metadata is not configured. |

### Urgency Values

| Urgency | Meaning | Action |
|---------|---------|--------|
| `page_now` | Immediate human intervention required | Page on-call engineer immediately, regardless of time |
| `notify_channel` | Timely awareness needed | Post to incident channel; team expected to respond within 30 minutes |
| `business_hours` | Can wait for normal working hours | Queue for review during next business day |

### Error Response

```json
{
  "status": "error",
  "message": "Escalation decision failed: severity field missing from input",
  "data": null
}
```

---

## Escalation Heuristics

These heuristics guide the escalation decision. Apply them in the specified order — each step may override or elevate the urgency determined by prior steps.

### Step 1: Severity-to-Urgency Baseline Mapping

Map the incident severity to an initial baseline urgency:

| Severity | Baseline Urgency | Rationale |
|----------|-----------------|-----------|
| `critical` | `page_now` | Critical incidents always start at the highest urgency |
| `high` | `notify_channel` | High severity warrants prompt team awareness |
| `medium` | `business_hours` | Medium severity can typically wait for normal hours |
| `low` | `business_hours` | Low severity is informational and non-urgent |

This is the starting point. Subsequent rules may **increase** urgency but never decrease it below the baseline.

### Step 2: Blast Radius Escalation

Evaluate the scope of impact. If the blast radius spans multiple services, increase urgency by one level.

**Rule**: If `blastRadius.affectedServices` contains **more than one** service, increase urgency by one level:
- `business_hours` → `notify_channel`
- `notify_channel` → `page_now`
- `page_now` → `page_now` (already at maximum)

**Rationale**: Multi-service impact indicates a systemic issue that is likely to cascade and requires coordinated response.

**Application**:
- Count distinct entries in `blastRadius.affectedServices`
- A single service with many pods affected does NOT trigger this rule — it must be multiple distinct services
- If `affectedServices` is empty or contains only one entry, skip this step

### Step 3: Time-of-Day Escalation

Evaluate whether the incident occurs outside business hours. Off-hours incidents with high+ severity are more urgent because response times are naturally longer and teams may not be monitoring channels.

**Rule**: If the current local time (in `teamTimezone`) is **outside** business hours (before 09:00 or at/after 17:00) AND severity is `high` or `critical`, increase urgency by one level:
- `business_hours` → `notify_channel`
- `notify_channel` → `page_now`
- `page_now` → `page_now` (already at maximum)

**Business hours definition**: 09:00–17:00 (inclusive of 09:00, exclusive of 17:00) in the configured team timezone, Monday through Sunday (weekday/weekend distinction is not applied — use time-of-day only).

**Application**:
- Convert `currentTimeUtc` to `teamTimezone` to determine local hour
- Only apply this escalation when severity is `high` or `critical` — medium/low incidents outside hours do not warrant elevated urgency
- If timezone conversion fails, assume outside business hours (err on the side of caution)

### Step 4: Service Criticality Escalation

If service criticality metadata is available, apply tier-based escalation overrides.

**Rule**: If any affected service is marked as `tier-1` in `serviceCriticality` AND severity is `high` or `critical`, set urgency to `page_now` regardless of current urgency level.

**Application**:
- Check each service in `blastRadius.affectedServices` against the `serviceCriticality` map
- If any match is `tier-1` AND severity is at least `high`, force `page_now`
- If `serviceCriticality` is null or no services match, skip this step
- Tier-2 and lower services do not trigger this override

**Rationale**: Tier-1 services are revenue-critical or customer-facing systems where any degradation at high+ severity demands immediate response.

### Step 5: Escalation Flag Determination

Set the `escalate` boolean based on final urgency:
- `page_now` → `escalate: true`
- `notify_channel` → `escalate: true`
- `business_hours` → `escalate: false`

---

## Threshold Definitions

### Severity Levels

| Level | Description | Examples |
|-------|-------------|----------|
| `critical` | Complete service outage or data loss risk | All pods down, database unreachable, 100% error rate |
| `high` | Significant degradation affecting users | >50% error rate, major latency spike, partial outage |
| `medium` | Noticeable issue with limited impact | Elevated error rate, single pod crashes, addon degraded |
| `low` | Minor anomaly, informational | Warning-level metric deviation, non-critical log errors |

### Business Hours

- **In hours**: 09:00 ≤ local time < 17:00 in team timezone
- **Out of hours**: local time < 09:00 OR local time ≥ 17:00

### Multi-Service Blast Radius

- **Single service**: `affectedServices.length == 1` — no blast radius escalation
- **Multi-service**: `affectedServices.length > 1` — triggers escalation bump

### Service Criticality Tiers

| Tier | Description | Escalation Impact |
|------|-------------|-------------------|
| `tier-1` | Revenue-critical, customer-facing, SLA-bound | Forces `page_now` at high+ severity |
| `tier-2` | Important internal services, non-customer-facing | No special override |
| `tier-3` | Development, testing, non-production | No special override |

---

## Reason Generation

The `reason` field must be non-empty and must reference:
1. **The severity level** — Always mention the severity classification
2. **The primary influencing factor** — Identify which factor most influenced the final urgency:
   - If blast radius caused the escalation bump: mention the number of affected services
   - If time-of-day caused the escalation bump: mention that the incident is outside business hours
   - If service criticality caused the override: mention the tier-1 service by name
   - If baseline severity alone determined urgency (no bumps applied): state that severity alone warrants the classification

### Reason Templates

Use these as guidance (not verbatim):

- **Critical baseline, no bumps**: "Critical severity incident warrants immediate paging. [N] pods affected in [service]."
- **High + multi-service**: "High severity incident with blast radius spanning [N] services ([list]). Multi-service impact increases urgency to page_now."
- **High + off-hours**: "High severity incident occurring outside business hours ([time] [timezone]). Off-hours timing increases urgency to page_now."
- **High + tier-1**: "High severity incident affecting tier-1 service [name]. Critical service classification requires immediate paging."
- **Medium, no bumps**: "Medium severity incident affecting [service]. Impact is contained and can be addressed during business hours."
- **Incomplete data**: "High severity incident with incomplete assessment (unavailable: [sources]). Escalating based on available signals; missing data noted."

### Incomplete Data Handling

When `unavailableSources` is non-empty:
- Still produce a valid urgency classification using available data
- Append to the reason field: mention which sources were unavailable
- Do NOT reduce urgency due to missing data — err on the side of higher urgency when uncertain
- If severity and blast radius data are both unavailable, default to `notify_channel` with reason explaining the uncertainty

---

## Output Interpretation

### For the Orchestrator

After receiving the `EscalationDecision`:

1. **Include in IncidentReport** — Set `incidentReport.escalation` to the returned decision
2. **Pass to incident-summary-format** — The escalation urgency, reason, and suggested responders are displayed in the Slack message escalation section

### For Engineers

- **`page_now`**: Expect an immediate page. This is reserved for incidents that cannot wait — critical severity, multi-service outage, or tier-1 service degradation.
- **`notify_channel`**: Check the incident channel promptly (within 30 minutes). The incident is significant but not catastrophic.
- **`business_hours`**: Review when convenient during working hours. The incident is contained, low-impact, or informational.

### Suggested Responders

The `suggestedResponders` field provides team/role names that should be notified:
- Derive from `serviceCriticality` metadata (on-call team for the affected tier-1 service)
- If no metadata is available, return an empty list (the orchestrator or Slack routing handles default on-call)
- Include at most 3 responder teams to avoid notification fatigue

---

## MCP Dependencies

This skill has **no direct MCP dependencies**. It operates entirely on pre-computed data passed in via the `EscalationInput`.

The script performs:
- Severity-to-urgency baseline mapping
- Time-of-day evaluation (timezone conversion)
- Blast radius scope checking
- Service criticality lookup

All data access is performed by upstream skills and the orchestrator before this skill is invoked.

---

## Error Handling

The script handles failures gracefully:

- **Missing severity field**: Return error response — severity is required for any escalation decision
- **Missing blast radius**: Use severity-only baseline mapping; note in reason that blast radius was unavailable
- **Invalid timezone**: Assume outside business hours (conservative escalation); note timezone error in reason
- **Empty affectedServices list**: Skip blast radius escalation step; proceed with other factors
- **Null serviceCriticality**: Skip tier-based override; proceed with severity + blast radius + time-of-day

The orchestrator should always include an escalation decision in the report. If this skill fails entirely, the orchestrator should default to `notify_channel` with reason "Escalation decision skill unavailable — defaulting to channel notification."

---

## Constraints

- Urgency must be one of exactly three values: `page_now`, `notify_channel`, `business_hours` (Requirement 8.3)
- Reason must be non-empty and reference severity and the primary influencing factor (Requirement 8.4)
- Severity baseline mapping: critical→page_now, high→notify_channel, medium/low→business_hours (Requirement 8.1)
- Urgency increases by one level when blast radius spans multiple services (Requirement 8.2)
- Urgency increases by one level when outside business hours with high+ severity (Requirement 8.2)
- Tier-1 services with high+ severity always result in page_now (Requirement 8.5)
- Incomplete data must still produce a valid classification with missing sources noted in reason (Requirement 8.6)
- Urgency can only increase through escalation steps, never decrease below baseline

---

## Validates

- **Requirement 8.1**: Severity-to-urgency baseline mapping (critical→page_now, high→notify_channel, medium/low→business_hours)
- **Requirement 8.2**: Urgency increased by one level for multi-service blast radius or off-hours with high+ severity
- **Requirement 8.3**: Urgency classified as one of page_now, notify_channel, or business_hours
- **Requirement 8.4**: Non-empty reason referencing severity and primary influencing factor
- **Requirement 8.5**: Tier-1 critical services with high+ severity always result in page_now
- **Requirement 8.6**: Incomplete data still produces valid classification with missing sources noted
- **Requirement 14.2**: SKILL.md contains trigger conditions, input/output contracts, reasoning heuristics
- **Requirement 14.10**: Prompt-heavy classification documented
