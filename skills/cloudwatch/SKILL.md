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
│  → Check alarm configuration (threshold, dimensions, evaluation periods)
│  → Pull the underlying metric and compare to baseline
│  → Assess: real anomaly, normal variance, or alarm misconfiguration?
│
├─ "Check the logs" / "Are there errors?"
│  → Identify log group(s) (describe_log_groups if needed)
│  → Run analyze_log_group for anomalies and patterns
│  → If specific query needed: execute_log_insights_query → get_logs_insight_query_results
│  → For multi-region or multi-group: execute_cwl_insights_batch
│
├─ "What's the trend?" / Capacity planning
│  → Pull extended baseline (7d or 14d)
│  → Use analyze_metric for trend and seasonality
│  → Identify growth patterns, cyclical behavior, or drift
│
└─ "Is this alarm configured correctly?"
   → Get alarm details
   → Check dimensions, threshold vs actual values, evaluation periods, actions
   → Compare threshold against get_recommended_metric_alarms
   → Recommend fixes
```

## Investigation Patterns

### Pattern: Alarm Assessment

When an alarm fires, answer these questions in order:

1. **What triggered it?** Get the alarm history. Find the triggering datapoint and time.
2. **Is the metric actually anomalous?** Pull 1h current and 24h baseline. Compare means, p95, max. If current is within 1 standard deviation of baseline, it's likely noise.
3. **Is the alarm well-configured?** Check:
   - Does it have specific dimensions (InstanceId, ServiceName) or is it an undimensioned aggregate?
   - Is the threshold realistic vs the metric's normal range?
   - Does it require sustained breach (multiple evaluation periods) or fires on a single datapoint?
   - Has it been in ALARM continuously since creation (never recovered)?
4. **What's the conclusion?** One of:
   - Real anomaly: metric genuinely deviated. Describe the deviation and timeframe.
   - Normal variance: metric fluctuates into this range regularly. Show the baseline.
   - Alarm misconfiguration: threshold too low, missing dimensions, or insufficient evaluation. Explain what's wrong.
   - Insufficient data: can't determine. Say what's missing.

### Pattern: Baseline Comparison

Standard baseline comparison for any metric:

1. Pull **current window** (last 1h, 1-min period, Average stat)
2. Pull **baseline window** (last 24h, 5-min period, Average stat)
3. Compute:
   - Current mean vs baseline mean
   - Current max vs baseline p95
   - Percent change from baseline
4. Classify:
   - < 2x baseline stddev → normal variance
   - 2-3x baseline stddev → elevated (worth noting)
   - > 3x baseline stddev → anomalous (investigate further)
   - > 5x baseline stddev → critical deviation

### Pattern: Log Error Investigation

1. Start with `analyze_log_group` — it finds anomalies and patterns automatically
2. If you need specific queries, use `execute_log_insights_query` with:
   ```
   fields @timestamp, @message
   | filter @message like /ERROR|Exception|Timeout/
   | sort @timestamp desc
   | limit 50
   ```
3. For counting errors over time:
   ```
   filter @message like /ERROR/
   | stats count(*) as errorCount by bin(5m)
   | sort bin(5m) desc
   ```
4. Always check: are these errors new (not in baseline) or pre-existing?

## Tool Reference

These are the CloudWatch MCP tools available. Run `--help` mentally before using each one:

| Tool | Use for |
|------|---------|
| `get_active_alarms` | Find currently firing alarms |
| `get_alarm_history` | See when an alarm fired, what triggered it, state transitions |
| `get_metric_data` | Pull metric values for a time range (the core investigation tool) |
| `get_metric_metadata` | Understand what a metric means, how it's calculated, what stats to use |
| `get_recommended_metric_alarms` | Get suggestions for how an alarm should be configured |
| `analyze_metric` | Determine trend, seasonality, and statistical properties |
| `describe_log_groups` | Find log groups by name pattern |
| `analyze_log_group` | Automatic anomaly and pattern detection in logs |
| `execute_log_insights_query` | Run a specific Logs Insights query |
| `get_logs_insight_query_results` | Get results from a query started with execute_log_insights_query |
| `execute_cwl_insights_batch` | Query across multiple log groups/regions in one call |

## What to Report

Always include:
- The specific numbers (current value, baseline value, percent deviation)
- Timestamps (when did the anomaly start, when was the alarm triggered)
- Your assessment (real problem vs noise vs misconfiguration)
- What you checked and what you couldn't verify
- Recommended action (fix the alarm, investigate the service, escalate, etc.)
