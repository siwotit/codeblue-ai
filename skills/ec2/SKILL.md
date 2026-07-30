---
name: ec2
description: Investigate an EC2 instance problem. Use when the engineer reports instance unreachability, status check failures, high CPU/memory, networking issues, or EC2-related alarms.
---

## Investigation Steps

1. Identify the instance ID(s) and region from the engineer's input
2. Describe the instance — state, instance type, launch time, security groups, subnet, VPC
3. Check status checks — system status (host hardware) and instance status (guest OS)
4. Pull CloudWatch metrics — CPUUtilization, NetworkIn/Out, StatusCheckFailed, EBSReadOps
5. Compare current metrics against 24h baseline — is this anomalous?
6. Check for recent changes via CloudTrail — was the instance stopped/started, modified, security group changed?
7. If networking is the issue, check security groups, NACLs, route tables
8. If part of an Auto Scaling group, check ASG health and scaling activity

## Key patterns

- **System status impaired**: Host hardware problem — needs stop/start to migrate to new host
- **Instance status impaired**: Guest OS problem — kernel panic, disk full, network misconfigured
- **CPU 100% sustained**: Runaway process or underprovisioned instance type
- **Network traffic drops to zero**: Security group change, NACL block, or ENI detached
- **StatusCheckFailed after reboot**: Boot issue — check console output if available

## What to report

- Instance state and status check results
- Current metrics vs baseline
- Whether a recent change caused the problem (with CloudTrail evidence)
- Recommended action (stop/start for host issue, resize for capacity, fix SG for network)
- What you couldn't check and what remains uncertain
