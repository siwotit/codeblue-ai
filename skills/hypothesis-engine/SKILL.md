# Hypothesis Engine Skill

**Classification**: prompt-heavy

The SKILL.md contains the primary reasoning logic — detailed heuristics for temporal alignment, resource overlap, error novelty, causal reasoning patterns, and confidence scoring. The script (`hypothesis_engine.py`) is minimal: it computes temporal overlap scores, formats the input context, and validates the output schema. The LLM performs the core hypothesis generation and evidence synthesis.

---

## When to Invoke (Trigger Conditions)

Invoke this skill when **all** of the following conditions are met:

1. **All signal-collection skills have completed** — The orchestrator has finished executing metric-baseline, deploy-correlation, log-triage, and (if applicable) k8s-cluster-health. Some may have returned errors — that's acceptable; invoke this skill with whatever data was successfully collected.
2. **An IncidentContext can be assembled** — At minimum, the original `NormalizedAlert` and at least one additional signal (metric deviation, correlated changes, log findings, or cluster health) are available.
3. **The orchestrator is in the `hypothesis` phase** — The workflow has transitioned past all data-collection phases.

**Skip conditions**: Never skip this skill. Even with minimal data, produce at least one hypothesis (which may be "Insufficient data for diagnosis" at low confidence).

---

## Input Contract

The script accepts an `IncidentContext` JSON object:

```json
{
  "alert": { "...NormalizedAlert..." },
  "metricDeviation": { "...MetricDeviation or null..." },
  "correlatedChanges": { "...CorrelatedChanges or null..." },
  "logFindings": { "...LogFindings or null..." },
  "clusterHealth": { "...ClusterHealthReport or null..." }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `alert` | NormalizedAlert | yes | The triggering alert with all context fields |
| `metricDeviation` | MetricDeviation \| null | no | Baseline comparison results. Null if metric-baseline skill failed or was skipped. |
| `correlatedChanges` | CorrelatedChanges \| null | no | Recent changes found in lookback window. Null if deploy-correlation skill failed. |
| `logFindings` | LogFindings \| null | no | Error patterns found in logs. Null if log-triage skill failed. |
| `clusterHealth` | ClusterHealthReport \| null | no | Kubernetes cluster state. Null if not an EKS incident or K8s skills failed. |

### Constructing Input from Orchestrator State

- `alert` → The `NormalizedAlert` produced by alert-ingestion
- `metricDeviation` → Output from metric-baseline skill (null if unavailable)
- `correlatedChanges` → Output from deploy-correlation skill (null if unavailable)
- `logFindings` → Output from log-triage skill (null if unavailable)
- `clusterHealth` → Output from k8s-cluster-health skill (null if not applicable or unavailable)

---

## Output Contract

The script returns a `RankedHypotheses` JSON object wrapped in a `SkillResponse` envelope:

```json
{
  "status": "success",
  "message": "Generated 3 ranked hypotheses for incident",
  "data": {
    "hypotheses": [
      {
        "rank": 1,
        "title": "Deployment of payments-service v2.4.1 caused memory leak",
        "description": "A deployment at 10:18 UTC introduced a memory leak...",
        "confidence": 0.85,
        "supportingEvidence": [
          {
            "claim": "Memory usage increased 4.2σ above baseline starting at 10:23 UTC",
            "source": "metric-baseline: AWS/ECS CPUUtilization",
            "timestamp": "2024-01-15T10:23:00Z",
            "weight": 0.9
          },
          {
            "claim": "Deployment of payments-service v2.4.1 occurred at 10:18 UTC",
            "source": "deploy-correlation: CloudTrail UpdateService",
            "timestamp": "2024-01-15T10:18:00Z",
            "weight": 0.85
          }
        ],
        "contradictingEvidence": [
          {
            "claim": "No OOMKilled pods observed in the payments namespace",
            "source": "k8s-cluster-health: pod status",
            "timestamp": "2024-01-15T11:00:00Z",
            "weight": 0.3
          }
        ],
        "suggestedVerification": [
          "Check pod memory usage trend: kubectl top pods -n payments --sort-by=memory",
          "Compare deployment diff: kubectl rollout history deployment/payments-service -n payments",
          "Review application heap dumps if available"
        ]
      }
    ],
    "unavailableSignals": ["clusterHealth"],
    "confidenceAdjustment": -0.2
  }
}
```

### RankedHypotheses Schema

| Field | Type | Description |
|-------|------|-------------|
| `hypotheses` | Hypothesis[] | 1–10 hypotheses, ordered by descending confidence, unique ranks |
| `unavailableSignals` | string[] | List of signal types that were null/unavailable |
| `confidenceAdjustment` | float | Total confidence reduction applied due to missing signals (-0.2 per missing source) |

### Hypothesis Schema

| Field | Type | Description |
|-------|------|-------------|
| `rank` | int | Unique rank (1 = most likely). No duplicates allowed. |
| `title` | string | One-line summary of the hypothesis |
| `description` | string | Detailed explanation of the causal chain |
| `confidence` | float | 0.0–1.0 confidence score (see scoring framework below) |
| `supportingEvidence` | EvidenceItem[] | At least 1 item with source reference required |
| `contradictingEvidence` | EvidenceItem[] | Evidence that weakens this hypothesis. If none found, include a single item with claim "No contradicting evidence found" |
| `suggestedVerification` | string[] | At least 1 step required. Commands or actions to confirm/refute. |

### EvidenceItem Schema

| Field | Type | Description |
|-------|------|-------------|
| `claim` | string | What this evidence asserts |
| `source` | string | Origin reference: `"skill-name: specific source"` format |
| `timestamp` | ISO8601 \| null | When the observation was made |
| `weight` | float | 0.0–1.0 how strongly this supports/contradicts the hypothesis |

### Error Response

```json
{
  "status": "error",
  "message": "Hypothesis generation failed: no signals available beyond the original alert",
  "data": null
}
```

---

## Reasoning Heuristics

These heuristics guide the LLM's reasoning when generating hypotheses from the collected signals. Apply them in order of importance.

### 1. Temporal Alignment Scoring

Temporal proximity between a change and symptom onset is the strongest initial signal for causation.

**Scoring rules**:
- **0–5 minutes** between change and anomaly start: **Very high** causal likelihood (weight 0.9–1.0). Changes immediately preceding symptom onset are the primary suspects.
- **5–15 minutes**: **High** likelihood (weight 0.7–0.9). Most deployment-induced issues manifest within this window.
- **15–60 minutes**: **Moderate** likelihood (weight 0.4–0.7). Slower-onset issues (memory leaks, connection pool exhaustion, gradual capacity saturation).
- **1–6 hours**: **Low** likelihood (weight 0.2–0.4). Possible for configuration drift, scheduled jobs, or cascading failures.
- **6–24 hours**: **Very low** likelihood (weight 0.05–0.2). Include only if no closer changes exist and resource overlap is strong.

**Application**:
- Use `metricDeviation.anomalyStartTime` as the anchor point (preferred over `alert.firedAt` because anomaly start is when the problem began, not when the threshold was breached)
- If `anomalyStartTime` is null, fall back to `alert.firedAt`
- For each change in `correlatedChanges.changes`, compute: `|change.timestamp - anomalyStartTime|`
- Weight temporal alignment exponentially — a 2-minute gap is dramatically more suspicious than a 30-minute gap

### 2. Resource Overlap Evaluation

Changes that directly affect the same resources as the alert are more likely causal than those affecting sibling or upstream resources.

**Scoring rules**:
- **Exact match**: Change `affectedResource` matches an alert `affectedResources[].value` directly → weight 1.0
- **Same namespace/service**: Change affects a different resource in the same namespace or service group → weight 0.6
- **Same cluster/account**: Change affects a resource in the same cluster but different namespace → weight 0.3
- **Upstream dependency**: Change affects a known dependency (e.g., a shared database, VPC component, or DNS) → weight 0.5
- **No overlap**: Change touches completely unrelated resources → weight 0.1

**Application**:
- Use `change.resourceOverlap` (boolean) as a quick filter: changes with `resourceOverlap: true` get priority
- For more nuanced scoring, compare `change.affectedResource` against all entries in `alert.affectedResources`
- Consider Kubernetes relationships: a node-level change affects all pods on that node; a namespace-level change affects all workloads in that namespace

### 3. Error Novelty Assessment

New errors (not present in baseline) are far more diagnostic than pre-existing errors with increased frequency.

**Scoring rules**:
- **New error pattern** (`logFinding.isNew == true`): weight 0.9 — this error did not exist before the incident. Strong diagnostic signal.
- **Existing error with >5x frequency increase**: weight 0.6 — pre-existing issue significantly amplified. May indicate a latent bug triggered by load or configuration change.
- **Existing error with 2–5x frequency increase**: weight 0.4 — moderate increase. Could be symptomatic or coincidental.
- **Existing error with <2x frequency increase**: weight 0.1 — likely noise. Normal variance in error rates.

**Application**:
- Compute frequency ratio: `logFinding.count / (logFindings.baselineErrorCount / timeWindowRatio)`
- Prioritize findings where `isNew == true` AND `severity == "error"` or `"fatal"`
- Cross-reference new error patterns with change timestamps: a new error appearing within 5 minutes of a deployment is strongly correlated

### 4. Contradicting Signal Detection

Actively seek evidence that weakens each hypothesis. Honest reporting of contradictions builds trust and helps engineers prioritize verification.

**Contradiction patterns**:
- **Metric normal but logs show errors**: The issue may be intermittent, below metric aggregation granularity, or affecting a subset of requests not captured by the metric.
- **Change correlates temporally but metrics were already degraded before the change**: The change may be coincidental; the root cause likely predates it.
- **Pod failures present but node conditions healthy**: Issue is likely application-level, not infrastructure.
- **Deployment found but error pattern is pre-existing**: The deployment didn't introduce the error; it may have amplified a latent issue.
- **High correlation score but no log evidence**: The change may not have had user-visible impact, or logs are incomplete.

**Application**:
- For every hypothesis, explicitly search for at least one contradicting signal
- If genuinely no contradicting evidence exists, record: `{"claim": "No contradicting evidence found", "source": "hypothesis-engine: exhaustive signal review", "weight": 0.0}`
- Higher-weighted contradictions should lower the hypothesis confidence score

### 5. Signal Absence Reasoning

Missing signals are themselves informative — they constrain the space of possible root causes.

**Rules**:
- **No changes found in 24h lookback**: Reduces likelihood of deployment-caused issues. Shift focus to infrastructure (capacity, upstream dependencies, external services).
- **Metrics normal despite alert firing**: Could be a noisy alarm, threshold misconfiguration, or a problem in a dimension not being monitored.
- **No new log errors**: Issue may be at the infrastructure/network layer (below application logging), or logs are not capturing the failure mode.
- **Cluster health normal but K8s workload failing**: Issue is likely application-specific, not cluster-wide.

---

## Confidence Scoring Framework

Confidence represents the probability that a hypothesis correctly identifies the root cause. It is derived from the strength and alignment of available evidence.

### Base Confidence Calculation

Start with a base confidence and adjust based on evidence:

| Condition | Base Confidence |
|---|---|
| Strong temporal alignment (0–5 min) + resource overlap + new errors | 0.85–0.95 |
| Moderate temporal alignment (5–15 min) + resource overlap | 0.65–0.80 |
| Temporal alignment only (no resource overlap or log correlation) | 0.40–0.55 |
| Resource overlap only (no temporal proximity) | 0.30–0.45 |
| Pattern match only (symptom matches known failure mode, no specific change identified) | 0.20–0.35 |
| Insufficient data / speculative | 0.10–0.20 |

### Confidence Modifiers

Apply these adjustments to the base confidence:

| Factor | Adjustment |
|---|---|
| Each supporting evidence item with weight > 0.7 | +0.05 (max +0.15 total) |
| Each contradicting evidence item with weight > 0.5 | -0.10 |
| Metric deviation classification is `critical` | +0.10 |
| Metric deviation classification is `normal` | -0.15 |
| Log findings include fatal-severity new errors | +0.10 |
| Multiple changes in close temporal proximity (ambiguity) | -0.10 |
| Missing signal source (per unavailable source) | -0.20 (min confidence 0.1) |
| Sparse baseline data (confidence capped at 0.5 from metric skill) | -0.10 |

### Confidence Bounds

- **Maximum**: 0.95 — never claim absolute certainty; there's always a possibility of incomplete data
- **Minimum**: 0.10 — even the weakest hypothesis has some informational value
- **Insufficient data threshold**: If no hypothesis can exceed 0.2 confidence, produce a single hypothesis titled "Insufficient data for diagnosis" with confidence 0.1, listing missing signal types

### Multi-Hypothesis Confidence Rules

- Hypotheses MUST be ordered by strictly descending confidence (rank 1 = highest confidence)
- No two hypotheses may have the same rank value
- When two hypotheses have confidence scores within 0.15 of each other, both MUST be reported with full evidence (per Requirement 11.5)
- Total confidence across all hypotheses need not sum to 1.0 — multiple independent root causes are possible

---

## Causal Reasoning Patterns

Apply these patterns to match observed signals to known failure modes. Each pattern describes a signature — a combination of signals that, when present together, suggest a specific root cause category.

### Pattern 1: Deployment-Induced Regression

**Signature**:
- Recent deployment or rollout (within 15 min of anomaly start)
- New error patterns in logs appearing after deployment timestamp
- Metric deviation beginning after deployment timestamp
- Resource overlap between deployment target and alert resource

**Confidence**: High (0.75–0.90) when all signals align

**Verification steps**:
- Compare deployment artifact diff (what changed in the code/config)
- Check rollback feasibility and impact
- Review deployment health checks and canary metrics

### Pattern 2: Resource Exhaustion

**Signature**:
- Gradually increasing metric deviation (not sudden spike)
- OOMKilled or MemoryPressure conditions in cluster health
- Log errors indicating connection pool exhaustion, thread starvation, or disk full
- No recent deployment (or deployment was days ago)

**Confidence**: Moderate-High (0.60–0.80) — harder to pinpoint exact trigger

**Verification steps**:
- Check resource limits vs actual usage: `kubectl top pods`, CloudWatch memory/CPU graphs
- Identify the resource being exhausted (memory, CPU, disk, connections, file descriptors)
- Determine growth rate to estimate when limit was reached

### Pattern 3: Infrastructure / Upstream Dependency Failure

**Signature**:
- Multiple unrelated services affected simultaneously
- No recent deployments for affected services
- Network-related errors in logs (connection refused, timeout, DNS failure)
- Node conditions showing NetworkUnavailable or NotReady

**Confidence**: Moderate (0.50–0.70) — often requires external validation

**Verification steps**:
- Check AWS Health Dashboard for regional issues
- Verify VPC, NAT Gateway, and security group configurations
- Test connectivity to upstream dependencies from an unaffected node

### Pattern 4: Configuration Change Impact

**Signature**:
- IAM change or config_change in correlated changes
- Permission-related errors in logs (AccessDenied, Unauthorized, 403)
- Temporal alignment with the config change timestamp
- Specific resource (role, policy, security group) appears in both change and error context

**Confidence**: High (0.70–0.85) when error messages directly reference the changed resource

**Verification steps**:
- Compare IAM policy before/after the change
- Test affected API calls with current permissions
- Review CloudTrail for the exact change event

### Pattern 5: Scaling Event / Capacity Issue

**Signature**:
- HPA or scaling event in correlated changes
- Pending pods or scheduling failures in cluster health
- Metrics showing elevated load (request rate, queue depth)
- No error-level log patterns, but increased latency

**Confidence**: Moderate (0.45–0.65) — scaling issues often self-resolve

**Verification steps**:
- Check current vs desired replica count: `kubectl get hpa`
- Review node capacity and pod resource requests
- Check if scaling limits (max replicas) have been reached

### Pattern 6: Addon / System Component Degradation

**Signature**:
- EKS addon in degraded or failed state (VPC CNI, CoreDNS, kube-proxy)
- Widespread pod communication failures across multiple namespaces
- Network-related errors not tied to any specific application
- Recent addon update in correlated changes

**Confidence**: Moderate-High (0.60–0.80) when addon status directly explains symptoms

**Verification steps**:
- Check addon versions and compatibility: `aws eks describe-addon`
- Review addon pod logs: `kubectl logs -n kube-system`
- Compare addon version with EKS cluster version compatibility matrix

---

## Output Interpretation

### For the Orchestrator

After receiving the `RankedHypotheses` output:

1. **Pass to evidence-provenance** — Each hypothesis's supporting evidence needs source links attached
2. **Pass to escalation-decision** — The top hypothesis confidence and blast radius inform escalation urgency
3. **Pass to incident-summary-format** — All hypotheses are included in the final Slack message

### For Engineers

- **confidence > 0.8**: Lead with this hypothesis in the summary. High certainty — the evidence strongly supports this explanation.
- **confidence 0.5–0.8**: Present as primary but note caveats. The hypothesis is plausible and the best explanation available, but verification is needed.
- **confidence < 0.5**: Present as secondary/speculative. Emphasize verification steps. Multiple competing explanations may exist.
- **Multiple hypotheses within 0.15 confidence of each other**: Indicate ambiguity. Present all contenders with their respective evidence and contradictions.

### Handling Insufficient Data

When `unavailableSignals` is non-empty:
- Reduce overall confidence per the scoring framework (-0.2 per missing source)
- Explicitly state which signals are missing in the hypothesis description
- Suggest targeted investigation steps to fill the gaps (e.g., "Check CloudTrail manually — deploy-correlation was unavailable")

---

## MCP Dependencies

This skill has **no direct MCP dependencies**. It operates entirely on pre-collected signals passed in via the `IncidentContext`.

The script performs:
- Temporal overlap computation (between change timestamps and anomaly start)
- Input structuring for LLM reasoning
- Output schema validation

All data access is performed by upstream skills (metric-baseline, deploy-correlation, log-triage, k8s-cluster-health).

---

## Error Handling

The script handles failures gracefully:

- **All signals null (only alert available)**: Produce a single hypothesis "Insufficient data for diagnosis" with confidence 0.1 and list all missing signal types
- **Output validation failure**: If generated hypotheses fail schema validation (missing evidence, duplicate ranks, non-descending confidence), re-attempt generation with explicit constraints
- **Malformed input**: Return error response indicating which fields are malformed

The orchestrator should always proceed to the reporting phase regardless of this skill's outcome — even a low-confidence hypothesis is better than no diagnosis.

---

## Constraints

- Produce between 1 and 10 hypotheses per incident (Requirement 6.8)
- All ranks must be unique integers starting from 1 (Requirement 6.7)
- Hypotheses must be ordered by strictly descending confidence (Requirement 6.2)
- Each hypothesis must have at least 1 supporting evidence item with source reference (Requirement 6.4)
- Each hypothesis must address contradicting evidence or explicitly state none found (Requirement 6.5)
- Each hypothesis must have at least 1 suggested verification step (Requirement 6.6)
- Confidence values must be in range [0.0, 1.0] (Requirement 6.3)
- When all available signals are insufficient for confidence > 0.2, produce single "Insufficient data" hypothesis at 0.1 (Requirement 6.9)

---

## Validates

- **Requirement 6.1**: Temporal overlap scoring for correlated changes vs anomaly start time
- **Requirement 6.2**: Hypotheses ranked by descending confidence
- **Requirement 6.3**: Confidence values bounded 0.0–1.0
- **Requirement 6.4**: Each hypothesis linked to at least one supporting evidence item with source reference
- **Requirement 6.5**: Contradicting evidence identified or explicitly stated as none found
- **Requirement 6.6**: At least one verification step per hypothesis
- **Requirement 6.7**: Unique rank values, no duplicates
- **Requirement 6.8**: Between 1 and 10 hypotheses per incident
- **Requirement 6.9**: Insufficient data produces single low-confidence hypothesis
- **Requirement 11.5**: Conflicting evidence produces multiple hypotheses (via multi-hypothesis confidence rules)
- **Requirement 14.2**: SKILL.md contains trigger conditions, input/output contracts, reasoning heuristics
- **Requirement 14.10**: Prompt-heavy classification documented
