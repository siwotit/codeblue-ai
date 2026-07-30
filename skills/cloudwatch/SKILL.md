---
name: cloudwatch
description: Investigate a CloudWatch alarm or metric anomaly. Use when the engineer reports an alarm firing, a metric spike, or asks about CloudWatch metrics/logs for any AWS resource.
---

## Investigation Steps

1. Identify the alarm or metric from the engineer's input (alarm name, metric name, namespace, dimensions, region)
2. Check alarm state and history — when did it fire? Has it fired before? What triggered it?
3. Pull current metrics at 1-minute resolution for the last hour
4. Pull 24h or 7-day baseline for comparison — is this anomalous or normal variance?
5. Check alarm configuration — is the threshold reasonable? Are dimensions set? How many evaluation periods?
6. If logs are relevant, query CloudWatch Logs Insights for error patterns in the same time window
7. Correlate: does the metric anomaly align with any other signal?

## What to report

- Current value vs baseline (with numbers)
- Whether this is a real anomaly or noise
- If it's alarm misconfiguration, explain what's wrong and suggest fixes
- If it's a real issue, describe what you found and recommend next steps
- What you couldn't check and what remains uncertain
