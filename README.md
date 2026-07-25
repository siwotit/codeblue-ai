# CodeBlue AI

An AI-powered incident first responder agent that runs inside Claude Code. It monitors your AWS infrastructure, detects anomalies before they become outages, and delivers actionable diagnoses to Slack — so you wake up with answers, not just alerts.

---

## What It Does

CodeBlue AI acts as your always-on first responder. When an incident is developing — or has already fired — it:

1. **Detects** — Reads CloudWatch Alarms, Grafana alert rules, and Kubernetes cluster state to catch issues as they emerge
2. **Correlates** — Pulls recent deployments, CloudTrail events, Kubernetes events, and metric baselines to find what changed
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
│  │                    CodeBlue AI Skills                           │   │
│  │                                                                 │   │
│  │  • alert-ingestion       • metric-baseline                      │   │
│  │  • deploy-correlation    • log-triage                           │   │
│  │  • hypothesis-engine     • incident-summary-format              │   │
│  │  • escalation-decision   • evidence-provenance                  │   │
│  │  • k8s-cluster-health    • pod-failure-triage                   │   │
│  │  • node-condition-check  • eks-addon-status                     │   │
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
| Deployment Tracking | CloudTrail + Kubernetes Events + (GitHub/CI optional) |

---

## MCPs (Access Layer)

| MCP | Purpose |
|-----|---------|
| `cloudwatch-mcp` | Read alarms, query metrics, pull log insights |
| `grafana-mcp` | Read dashboards, alert rules, and metric panels |
| `kubernetes-mcp` | Read pod/node/event state, describe resources, check EKS cluster health (read-only) |
| `slack-mcp` | Post incident summaries and triage updates |
| `aws-api-mcp` | CloudTrail events, EKS API, resource state, recent changes |

---

## Skills (Knowledge Layer)

| Skill | What It Does |
|-------|--------------|
| `alert-ingestion` | Normalize alerts from CloudWatch and Grafana into a common format |
| `metric-baseline-comparison` | Compare current metric values against 7-day/30-day baselines to quantify deviation |
| `deploy-correlation` | Check CloudTrail, Kubernetes rollouts, and deployment pipelines for changes in the last 24h that overlap with affected resources |
| `log-triage` | Query CloudWatch Logs Insights and pod logs for error patterns, exceptions, and timeout spikes |
| `hypothesis-engine` | Given correlated signals, produce a ranked list of probable root causes |
| `incident-summary-format` | Structure the output as a Slack message: severity, blast radius, hypothesis, evidence, and next steps |
| `escalation-decision` | Determine whether this needs immediate human intervention or can wait for business hours |
| `evidence-provenance` | Every claim in the summary links back to its source (metric, log line, CloudTrail event, kubectl output) |
| `k8s-cluster-health` | Check node conditions, pending pods, resource pressure, and cluster-level events |
| `pod-failure-triage` | Classify pod failures — CrashLoopBackOff, OOMKilled, ImagePullBackOff, sandbox creation errors — and surface the root cause |
| `node-condition-check` | Detect NotReady nodes, memory/disk pressure, PID pressure, and network unavailability |
| `eks-addon-status` | Check EKS addon health (VPC CNI, CoreDNS, kube-proxy, EBS CSI) for degraded or failed states |

---

## Workflow

```
Alert fires (CloudWatch Alarm → ALARM state / Grafana alert / K8s event)
        │
        ▼
CodeBlue AI picks up the alarm
        │
        ▼
Pull 1h of metrics for the affected resource (CloudWatch + Grafana)
        │
        ▼
Compare against 7-day baseline — is this anomalous or normal variance?
        │
        ▼
If EKS: check cluster health — node conditions, pending pods, failing addons, recent events
        │
        ▼
Query CloudTrail: any deployments, config changes, or IAM changes in the last 24h?
        │
        ▼
If K8s workload: check recent rollouts, replica changes, HPA activity, OOM events
        │
        ▼
Query CloudWatch Logs + pod logs: error rate spikes, new exception patterns?
        │
        ▼
Correlate: does the timeline of the metric change align with a deploy or config change?
        │
        ▼
Generate hypothesis with confidence level and supporting evidence
        │
        ▼
Post to Slack:
  🔴 INCIDENT — [Service] [Cluster] [Namespace] [Region]
  Severity: High
  Blast Radius: 12 pods in CrashLoopBackOff across 3 nodes in us-east-1
  Hypothesis: EBS CSI driver addon degraded after v1.37.0 upgrade at 14:32 UTC —
              pods cannot mount PVCs (evidence: 47 FailedAttachVolume events since 14:35)
  Evidence: [links to metrics, pod events, addon status, CloudTrail]
  Recommended: Roll back EBS CSI addon to v1.36.0 or cordon affected nodes
  Escalate: Yes — page on-call SRE
```

---

## What Makes This Different

- **Proactive, not reactive** — It reads alarm states continuously and can flag degradation before a full outage
- **Evidence-based** — Every hypothesis cites its source. No hallucinated root causes
- **Read-only** — It will never touch your infrastructure. Diagnosis only
- **Composable** — Skills are modular. Swap `deploy-correlation` for your CI tool, or add `terraform-drift-detection` later
- **Runs where you already work** — Claude Code on your terminal. No new SaaS platform to adopt

---

## Roadmap

- [ ] v0.1 — CloudWatch Alarm reader + metric baseline comparison + Slack output
- [ ] v0.2 — Grafana integration (dashboards + alert rules)
- [ ] v0.3 — EKS cluster health checks (node conditions, pod status, addon health)
- [ ] v0.4 — CloudTrail deploy correlation + K8s rollout detection
- [ ] v0.5 — Log triage (CloudWatch Logs Insights + pod logs)
- [ ] v0.6 — Hypothesis engine (multi-signal correlation)
- [ ] v1.0 — Full loop: detect → correlate → diagnose → report

---

## Future Extensions

- Karpenter/Cluster Autoscaler scaling event correlation
- Network Policy conflict detection (from your CNP case experience)
- VPC CNI IP exhaustion prediction
- PagerDuty/Opsgenie integration for bi-directional incident management
- GitHub MCP for commit-level deploy correlation
- Terraform state drift detection
- Multi-cluster support (fleet-wide triage)
- Multi-account support (AWS Organizations)
- Learning from resolved incidents — feedback loop to improve hypotheses
