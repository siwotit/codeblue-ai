---
name: cloudwatch
description: Investigate CloudWatch metrics, alarms, and logs. Use when the engineer asks about metric behavior, performance questions, log errors, alarm state, capacity trends, or needs to understand whether something is normal or anomalous.
---

# CloudWatch Investigation

## Decision Tree

```
Problem received → What type?
│
├─ "Is this normal?" / "Why is X high/low?"
│  → Identify metric name, namespace, dimensions
│  → Pull current window (get_metric_data, 1h at 1-min)
│  → Pull baseline (get_metric_data, 24h or 7d at 5-min)
│  → Use analyze_metric for trend/seasonality if pattern is unclear
│  → Compare and explain: anomalous, elevated, or normal variance
│
├─ "What happened at time T?" / "Something changed"
│  → Pull the relevant metric around that time window
│  → Compare the before and after periods
│  → Check logs around the same time (analyze_log_group or execute_log_insights_query)
│  → Look for correlated changes across multiple metrics if needed
│
├─ Alarm firing or alarm questions
│  → Get alarm details and history (get_active_alarms, get_alarm_history)
│  → Identify the metric breached and the resource from its dimensions
│  → Pull that metric for the breach window and compare to baseline
│  → Report what the alarm says about the resource; hand over to the ec2 or eks skill for resource-level checks
│
├─ "Check the logs" / "Are there errors?"
│  → Identify log group(s) (describe_log_groups if needed)
│  → Run analyze_log_group for anomalies and patterns
│  → If specific query needed: execute_log_insights_query → get_logs_insight_query_results
│  → For multi-region or multi-group: execute_cwl_insights_batch
│
└─ "What's the trend?" / Capacity planning
   → Pull extended baseline (7d or 14d)
   → Use analyze_metric for trend and seasonality
   → Identify growth patterns, cyclical behavior, or drift
```

## Investigation Patterns

### Pattern: Alarm Investigation

Treat the alarm as a signal: something crossed a threshold. Find out what it says about the resource.

1. **What did it breach?** Get the alarm and its history. Note the metric and namespace, the threshold and comparison, the statistic and period, and when it went into ALARM (and whether it still is).
2. **Which resource?** Read the dimensions (InstanceId, AutoScalingGroupName, ClusterName, VolumeId and so on) and identify the resource and its type.
3. **What does the metric say about that resource?** Pull the metric for the window around the breach and a 24h baseline. Report the breach size (value vs threshold vs baseline), when it started, how long it lasted, and whether it is still going.
4. **What else moved at the same time?** Pull related metrics on the same resource (for an instance: CPU, EBS throttling, network) and logs around the breach time.
5. **Hand over when the resource needs its own checks.**
   - Instance-level cause (status checks, CPU, EBS, network, console output): hand over to the `ec2` skill with the instance ID, the time window, and the breach.
   - Node, nodegroup or cluster context: hand over to the `eks` skill.
6. **State what the alarm says.** The resource, the metric, the size and timing of the breach, the likely cause if the evidence supports one, and what is still unknown. If the breach is small and brief, say so and show the baseline, but do not stop at "noise" without saying what the resource was doing.

### Pattern: Log Error Investigation

**Make log searches case-insensitive.** Logs Insights is case-sensitive by default. Put `(?i)` at the start of every regex, for example `/(?i)error/`, so `error`, `Error` and `ERROR` all match.

1. Start with `analyze_log_group` to find anomalies and patterns automatically
2. If you need specific queries, use `execute_log_insights_query` with:
   ```
   fields @timestamp, @message
   | filter @message like /(?i)error|exception|timeout/
   | sort @timestamp desc
   | limit 50
   ```
3. For counting errors over time:
   ```
   filter @message like /(?i)error/
   | stats count(*) as errorCount by bin(5m)
   | sort bin(5m) desc
   ```

## Tool Reference

These are the CloudWatch MCP tools available:

| Tool | Use for |
|------|---------|
| `get_active_alarms` | Find currently firing alarms |
| `get_alarm_history` | See when an alarm fired, what triggered it, state transitions |
| `get_metric_data` | Pull metric values for a time range (the core investigation tool). Supports percentiles (p50, p90, p99), math expressions and batching several metrics in one call |
| `get_metric_metadata` | Understand what a metric means, how it's calculated, what stats to use |
| `analyze_metric` | Determine trend, seasonality, and statistical properties |
| `describe_log_groups` | Find log groups by name pattern |
| `analyze_log_group` | Automatic anomaly and pattern detection in logs |
| `execute_log_insights_query` | Run a specific Logs Insights query |
| `get_logs_insight_query_results` | Get results from a query started with execute_log_insights_query |
| `cancel_logs_insight_query` | Stop a Logs Insights query that is still running |
| `execute_cwl_insights_batch` | Run one query across many log groups and regions in one call. It polls for completion and merges the results, so no separate results call is needed |

## What to Report

Always include:
- The resource and the metric (and the alarm, if one fired)
- The numbers: current value and baseline value, plus the threshold if an alarm fired
- Timestamps: when the breach or anomaly started, how long it lasted, and when the alarm triggered
- Your assessment: what the data says about the resource, and the likely cause if the evidence supports one
- What you couldn't verify, and how to check it
- Recommended action (investigate the resource, hand over to the ec2 or eks skill, escalate, etc.)
