# CodeBlue AI

An AI-powered incident first responder agent that runs inside Claude Code. It monitors your AWS infrastructure, detects anomalies before they become outages, and delivers actionable diagnoses to Slack — so you wake up with answers, not just alerts.

---

## What It Does

CodeBlue AI acts as your always-on first responder. When an incident is developing — or has already fired — it:

1. **Detects** — Reads CloudWatch Alarms, Grafana alert rules, and Kubernetes cluster state to catch issues as they emerge
2. **Correlates** — Pulls metrics, logs, cluster state, and recent rollouts to find what changed
3. **Diagnoses** — Produces a first-pass root cause hypothesis with supporting evidence
4. **Reports** — Posts a structured incident summary to Slack with severity, blast radius, and recommended next steps

All read-only. It never restarts services, rolls back deployments, or modifies infrastructure.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                      Claude Code (Agent Runtime)                      │
│                                                                      │
│  ┌───────────┐ ┌───────────┐ ┌────────────┐ ┌───────────────────┐   │
│  │ CloudWatch│ │  Grafana  │ │ Kubernetes │ │       Slack       │   │
│  │    MCP    │ │    MCP    │ │    MCP     │ │        MCP        │   │
│  └─────┬─────┘ └─────┬─────┘ └──────┬─────┘ └────────┬──────────┘   │
│        │              │              │                 │              │
│  ┌─────┴──────────────┴──────────────┴─────────────────┴──────────┐   │
│  │                    CodeBlue AI                                   │   │
│  │                                                                 │   │
│  │  Shared:                                                        │   │
│  │    • cloudwatch (metrics, logs, baseline comparison)            │   │
│  │    • models, config, mcp_client                                 │   │
│  │                                                                 │   │
│  │  Skills:                                                        │   │
│  │    • alert-ingestion        (entry point)                       │   │
│  │    • eks-triage             (service skill)                     │   │
│  │    • ec2-triage             (service skill)                     │   │
│  │    • ecs-triage             (service skill)                     │   │
│  │    • hypothesis-engine      (reasoning)                         │   │
│  │    • escalation-decision    (reasoning)                         │   │
│  │    • incident-summary       (output + provenance)               │   │
│  │                                                                 │   │
│  │  Future Service Skills:                                         │   │
│  │    • rds-triage                                                 │   │
│  │    • lambda-triage                                              │   │
│  └─────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Stack

| Component | Tool |
|-----------|------|
| Agent Runtime | Claude Code |
| Cloud Provider | AWS |
| Container Orchestration | Amazon EKS (Kubernetes) |
| Metrics & Dashboards | Grafana + CloudWatch |
| Alarms | CloudWatch Alarms |
| Notifications | Slack |

---

## MCPs (Access Layer)

| MCP | Purpose |
|-----|---------|
| `cloudwatch-mcp` | Read alarms, query metrics, pull log insights |
| `grafana-mcp` | Read dashboards, alert rules, and metric panels |
| `kubernetes-mcp` | Read pod/node/event state, describe resources, check EKS cluster health (read-only) |
| `slack-mcp` | Post incident summaries and triage updates |
| `aws-api-mcp` | CloudTrail events, EKS API, resource state |

---

## Skills

| Skill | Type | What It Does |
|-------|------|--------------|
| `alert-ingestion` | Entry point | Normalize alerts from CloudWatch, Grafana, and Kubernetes into a common format |
| `eks-triage` | Service skill | Full EKS/K8s diagnosis — cluster health, pod failures, node conditions, addon status, recent rollouts |
| `ec2-triage` | Service skill | EC2 instance diagnosis — status checks, health metrics, networking, recent changes, ASG context |
| `ecs-triage` | Service skill | ECS service/task diagnosis — service stability, task failures, circuit breaker, LB health, recent deployments |
| `hypothesis-engine` | Reasoning | Rank root causes by confidence from all collected signals |
| `escalation-decision` | Reasoning | Determine urgency: page_now / notify_channel / business_hours |
| `incident-summary` | Output | Format Slack Block Kit report with evidence provenance baked in |

### Shared Modules (not skills — imported by skills)

| Module | Provides |
|--------|----------|
| `shared/cloudwatch.py` | Metric queries, log queries, baseline comparison ("is this anomalous?") |
| `shared/models.py` | Pydantic data models (NormalizedAlert, MetricDeviation, etc.) |
| `shared/config.py` | Configuration and constants |
| `shared/mcp_client.py` | MCP tool call abstraction layer |

---

## Workflow

```
Alert fires (CloudWatch Alarm → ALARM state / Grafana alert / K8s event)
        │
        ▼
alert-ingestion (normalize to common format)
        │
        ▼
Signal collection (concurrent):
  ├── shared/cloudwatch: metric baseline comparison (is this anomalous?)
  ├── shared/cloudwatch: log triage (error patterns, exception spikes)
  ├── eks-triage: cluster health + pod failures + node conditions + addons + recent rollouts
  ├── ec2-triage: status checks + metrics + networking + recent instance changes
  └── ecs-triage: service stability + task failures + circuit breaker + LB health + deployments
        │
        ▼
hypothesis-engine (correlate all signals → ranked root causes)
        │
        ▼
escalation-decision (severity + blast radius + time-of-day → urgency)
        │
        ▼
incident-summary (format + provenance → Slack Block Kit message)
        │
        ▼
Post to Slack:
  🔴 INCIDENT — [Service] [Cluster] [Namespace] [Region]
  Severity: High
  Blast Radius: 12 pods in CrashLoopBackOff across 3 nodes
  Hypothesis: EBS CSI driver addon degraded after v1.37.0 upgrade —
              pods cannot mount PVCs (evidence: 47 FailedAttachVolume events)
  Evidence: [links to metrics, pod events, addon status]
  Recommended: Roll back EBS CSI addon to v1.36.0
  Escalate: Yes — page on-call SRE
```

---

## What Makes This Different

- **Proactive, not reactive** — Reads alarm states continuously and flags degradation before a full outage
- **Evidence-based** — Every hypothesis cites its source. No hallucinated root causes
- **Read-only** — Will never touch your infrastructure. Diagnosis only
- **Domain-organized** — Skills grouped by service (EKS, EC2, ECS) with CloudWatch as a shared utility
- **Composable** — Add `ec2-triage` or `ecs-triage` following the same pattern
- **Runs where you already work** — Claude Code on your terminal. No new SaaS platform

---

## Roadmap

- [x] v0.1 — CloudWatch Alarm reader + metric baseline comparison + Slack output
- [x] v0.2 — EKS cluster health checks (node conditions, pod status, addon health)
- [ ] v0.3 — Grafana integration (dashboards + alert rules)
- [ ] v0.4 — Log triage (CloudWatch Logs Insights + pod logs)
- [ ] v0.5 — Hypothesis engine (multi-signal correlation)
- [ ] v0.6 — EC2 triage skill
- [ ] v0.7 — ECS triage skill
- [ ] v1.0 — Full loop: detect → correlate → diagnose → report

---

## Future Extensions

- Karpenter/Cluster Autoscaler scaling event correlation
- Network Policy conflict detection
- VPC CNI IP exhaustion prediction
- PagerDuty/Opsgenie integration for bi-directional incident management
- GitHub MCP for commit-level deploy correlation
- Terraform state drift detection
- Multi-cluster support (fleet-wide triage)
- Multi-account support (AWS Organizations)
- Learning from resolved incidents — feedback loop to improve hypotheses
