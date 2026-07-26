# Incident Summary Format Skill

## Classification
**Mixed** — Balanced script + prompt guidance. The script handles deterministic Slack Block Kit structure assembly, severity-to-emoji mapping, and metadata attachment. The SKILL.md guides section ordering, content emphasis decisions, and contextual formatting choices.

## Trigger Conditions

Invoke this skill when:
- The **hypothesis generation phase** has completed and ranked hypotheses are available
- The **escalation decision** has been made (urgency classification is present)
- The **evidence provenance** skill has annotated claims with source links
- The orchestrator enters the reporting phase (final step before Slack delivery)

Do NOT invoke when:
- Hypothesis generation has not yet completed
- The workflow is still in signal-collection phases (metric-baseline, deploy-correlation, log-triage)
- An interactive triage session is producing intermediate responses (use TriageResponse format instead)

## Input Contract

The script accepts JSON via stdin conforming to the `IncidentReport` schema:

```json
{
  "incidentId": "string (required) — unique incident identifier",
  "generatedAt": "ISO8601 (required) — report generation timestamp",
  "processingDuration": "string (required) — elapsed time from alert to report",
  "alert": {
    "id": "string",
    "source": "cloudwatch | grafana | kubernetes",
    "severity": "critical | high | medium | low",
    "title": "string",
    "firedAt": "ISO8601",
    "region": "string",
    "cluster": "string (optional)",
    "namespace": "string (optional)"
  },
  "severity": "critical | high | medium | low",
  "blastRadius": {
    "summary": "string",
    "affectedServices": ["string"],
    "affectedNamespaces": ["string"],
    "affectedNodes": ["string"],
    "impactedPodCount": "number",
    "region": "string"
  },
  "hypotheses": [
    {
      "rank": "number",
      "title": "string",
      "description": "string",
      "confidence": "number (0.0–1.0)",
      "supportingEvidence": [
        {
          "claim": "string",
          "source": "string",
          "timestamp": "ISO8601 (optional)",
          "consoleUrl": "string (optional)",
          "verificationCommand": "string (optional)"
        }
      ],
      "contradictingEvidence": [],
      "suggestedVerification": ["string"]
    }
  ],
  "confidence": "number (0.0–1.0) — overall confidence in top hypothesis",
  "recommendedActions": ["string"],
  "escalation": {
    "escalate": "boolean",
    "reason": "string",
    "urgency": "page_now | notify_channel | business_hours",
    "suggestedResponders": ["string"]
  },
  "evidenceLinks": [
    {
      "claim": "string",
      "source": "string",
      "consoleUrl": "string (optional)",
      "verificationCommand": "string (optional)"
    }
  ]
}
```

### Field Descriptions
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `incidentId` | string | Yes | Unique identifier for this incident |
| `generatedAt` | ISO8601 | Yes | When the report was generated |
| `processingDuration` | string | Yes | Time elapsed from alert receipt to report generation |
| `alert` | NormalizedAlert | Yes | The original normalized alert that triggered the workflow |
| `severity` | string | Yes | Overall assessed severity (may differ from alert severity) |
| `blastRadius` | BlastRadius | Yes | Scope of incident impact |
| `hypotheses` | Hypothesis[] | Yes | Ranked root cause hypotheses (1–10) |
| `confidence` | number | Yes | Overall confidence in the top hypothesis (0.0–1.0) |
| `recommendedActions` | string[] | No | Suggested next steps for responders |
| `escalation` | EscalationDecision | No | Escalation recommendation with urgency |
| `evidenceLinks` | EvidenceItem[] | No | Provenance-annotated evidence with source links |

## Output Contract

The script returns a `SlackMessage` JSON to stdout wrapped in a `SkillResponse`:

```json
{
  "status": "success" | "error",
  "message": "string — empty on success, error description on failure",
  "data": {
    "channel": "string — target Slack channel",
    "text": "string — plain-text fallback summary for non-Block-Kit contexts",
    "blocks": [
      {
        "type": "header",
        "text": { "type": "plain_text", "text": "string" }
      },
      {
        "type": "section",
        "text": { "type": "mrkdwn", "text": "string" }
      }
    ],
    "metadata": {
      "incidentId": "string",
      "severity": "string",
      "generatedBy": "codeblue-ai"
    }
  }
}
```

## Slack Block Kit Formatting Rules

### Severity Emoji Mapping

| Severity | Emoji | Display |
|----------|-------|---------|
| critical | 🔴 | 🔴 CRITICAL |
| high | 🟠 | 🟠 HIGH |
| medium | 🟡 | 🟡 MEDIUM |
| low | 🟢 | 🟢 LOW |

### Required Block Kit Sections (in order)

The formatted message MUST include the following sections in this order:

1. **Header** — Incident title with severity emoji prefix
   - Format: `{emoji} {alert.title}`
   - Block type: `header`

2. **Severity & Status** — Severity level with processing metadata
   - Format: `*Severity:* {emoji} {severity} | *Confidence:* {confidence}%`
   - Include incident ID and processing duration
   - Block type: `section`

3. **Blast Radius** — Scope of impact
   - Format: `*Blast Radius:* {blastRadius.summary}`
   - List affected services, namespaces, nodes, pod count
   - Block type: `section`

4. **Root Cause Hypothesis** — Top-ranked hypothesis with confidence
   - Format: `*Most Likely Cause:* {hypotheses[0].title} ({confidence}% confidence)`
   - Include brief description from top hypothesis
   - If confidence < 0.5, add caveat: "⚠️ Low confidence — multiple causes possible"
   - Block type: `section`

5. **Evidence Links** — Source references for verification
   - List up to 5 evidence items with clickable links or commands
   - Format each as: `• {claim} — <{consoleUrl}|View in Console>` or `` • {claim} — `{verificationCommand}` ``
   - Block type: `section`

6. **Recommended Next Steps** — Actionable items for responders
   - Numbered list of recommended actions
   - Include suggested verification steps from top hypothesis
   - Block type: `section`

7. **Escalation** — Escalation decision and rationale
   - Format based on urgency:
     - `page_now`: `🚨 *ESCALATION: PAGE NOW* — {reason}`
     - `notify_channel`: `⚡ *Notify Channel* — {reason}`
     - `business_hours`: `📋 *Business Hours* — {reason}`
   - Include suggested responders if available
   - Block type: `section`

8. **Metadata Footer** — CodeBlue AI identifier and timing
   - Format: `_Generated by CodeBlue AI | Incident {incidentId} | Processed in {processingDuration}_`
   - Block type: `context`

### Dividers

Insert `{"type": "divider"}` blocks between major sections (after header, after blast radius, before escalation, before footer) to improve visual readability.

### Plain-Text Fallback

The `text` field MUST contain a plain-text summary suitable for notification contexts that do not render Block Kit (push notifications, email digests). Format:

```
[{SEVERITY}] {alert.title}
Blast Radius: {blastRadius.summary}
Likely Cause: {hypotheses[0].title} ({confidence}% confidence)
Escalation: {urgency} — {reason}
Generated by CodeBlue AI | {incidentId} | {processingDuration}
```

## Interpretation Guidance

### Content Emphasis Decisions

When formatting the report, apply these guidelines:

1. **Severity drives urgency of language** — Critical incidents use bold, urgent phrasing. Low-severity incidents use informational tone.

2. **Confidence drives certainty of language** — High confidence (>0.8) uses definitive language ("caused by"). Low confidence (<0.5) uses hedging ("may be related to", "possible cause").

3. **Multiple hypotheses** — If the top two hypotheses have confidence within 0.15 of each other, present both in the hypothesis section with a note that the root cause is ambiguous.

4. **Missing data** — If any evidence section is null/empty (e.g., no cluster health because the alert is non-K8s), omit that section entirely rather than showing an empty block.

5. **Evidence prioritization** — Show the most diagnostic evidence first. Prefer new findings (isNew: true) over pre-existing patterns. Prefer evidence with console URLs over raw commands.

### Error Handling

- If the `IncidentReport` is missing the `hypotheses` field or it is empty, produce a report stating "No root cause hypothesis could be generated" with confidence 0.0
- If `blastRadius` is missing, omit the blast radius section and note the gap
- If `escalation` is missing, omit the escalation section
- Never fail silently — always produce at least a minimal report with the alert title and severity

### Channel Selection

The target Slack channel is read from the CodeBlue AI configuration (environment variable `CODEBLUE_SLACK_CHANNEL` or config file). The script uses this value to populate the `channel` field. If no channel is configured, return an error status.

