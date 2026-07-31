---
name: ec2
description: Investigate EC2 instance problems. Use when the engineer asks about instance health, connectivity, performance, status checks, security groups, networking, instance state, or capacity.
---

# EC2 Investigation

## Decision Tree

```
Problem received → What type?
│
├─ "Instance is unreachable" / connectivity issues
│  → Describe the instance (state, VPC, subnet, security groups)
│  → Check status checks (system + instance)
│  → Check CPUUtilization (is it at 100%? is it at 0%? both tell a story)
│  → Check network metrics (NetworkIn/Out — did traffic drop to zero?)
│  → Check security group rules (is the needed port open?)
│  → Check if instance is in correct subnet with route to target
│  → If recently changed: check CloudTrail for SG modifications
│
├─ "Status check failed" / instance impaired
│  → Describe instance status (system status vs instance status)
│  → System status impaired → host hardware problem (needs stop/start to migrate)
│  → Instance status impaired → guest OS problem (kernel panic, disk full, misconfigured network)
│  → Both impaired → likely full host failure
│  → Check EBS volume metrics:
│    - VolumeThroughputExceededCheck (AWS/EBS, per volume with InstanceId)
│    - VolumeIOPSExceededCheck (AWS/EBS, per volume with InstanceId)
│  → Check instance-level EBS limits:
│    - InstanceEBSIOPSExceededCheck (AWS/EC2, per instance)
│    - InstanceEBSThroughputExceededCheck (AWS/EC2, per instance)
│  → If EBS checks show exceeded: disk I/O bottleneck may be causing unresponsiveness
│  → Get console output (ec2:GetConsoleOutput) — look for OOM kills, kernel panics, disk errors
│  → Check if it was working before (StatusCheckFailed over 24h)
│
├─ "High CPU" / "Instance is slow" / performance issues
│  → Get CloudWatch metrics: CPUUtilization, NetworkIn/Out, EBSReadOps, EBSWriteOps
│  → Pull 24h baseline for comparison
│  → Check EBS throttling (these often explain high CPU/slowness):
│    - VolumeThroughputExceededCheck (AWS/EBS, per volume)
│    - VolumeIOPSExceededCheck (AWS/EBS, per volume)
│    - InstanceEBSIOPSExceededCheck (AWS/EC2, per instance)
│    - InstanceEBSThroughputExceededCheck (AWS/EC2, per instance)
│  → If CPU is sustained 100%: likely runaway process, EBS throttle causing I/O wait, or underprovisioned type
│  → If EBS checks show exceeded: storage bottleneck causing processes to block on I/O, which shows as high CPU wait
│  → If network is saturated: instance type network limit
│  → Check instance type limits vs observed usage
│  → For T-series: check CPUCreditBalance (zero credits = throttled to baseline)
│
├─ "What changed?" / instance behaving differently
│  → Check CloudTrail for recent events on this instance:
│    - ModifyInstanceAttribute (type change, SG change)
│    - StopInstances / StartInstances / RebootInstances
│    - AttachVolume / DetachVolume
│    - AuthorizeSecurityGroupIngress/Egress
│  → Compare metrics before and after the change time
│  → Identify what action caused the behavior shift
│
├─ "Instance won't boot" / stuck starting / can't SSH or SSM
│  → Check instance state (is it running, pending, or stopped?)
│  → Check status checks (system + instance)
│  → Get console output (ec2:GetConsoleOutput) — look for:
│    - Kernel panic or BUG messages
│    - "No space left on device" (disk full preventing boot)
│    - OOM killer messages ("Out of memory: Kill process")
│    - fsck errors (file system corruption)
│    - "Failed to start" messages for systemd services
│    - SSH/SSHD start failures ("Failed to start OpenSSH")
│    - SSM agent start failures
│    - cloud-init errors (user data script failure)
│    - Network interface configuration failures ("RTNETLINK" errors)
│  → Check EBS volume status (is the root volume attached and healthy?)
│  → Check ENI status (is the primary network interface attached?)
│  → Check recent changes: was the instance type changed? Was user data modified?
│  → If InsufficientInstanceCapacity: AZ has no capacity for this type, try another AZ or different type
│  → If emergency mode (console shows "Dependency failed" or "emergency mode"):
│    - Likely /etc/fstab has entries for volumes that are detached or don't exist
│    - Fix: add `nofail` option to secondary mount entries in fstab
│    - Fix method: EC2 Serial Console, AWSSupport-ExecuteEC2Rescue automation, or rescue instance
│  → If kernel panic (console shows "VFS: Unable to mount root fs"):
│    - Corrupted kernel or initramfs. Revert to previous kernel.
│  → If it was working before: what was the last change before it stopped booting?
│
├─ "Instance disappeared" / terminated unexpectedly
│  → Check CloudTrail for TerminateInstances — who did it and when?
│  → Check if part of ASG — was it terminated by health check failure?
│  → Check if Spot instance — look for Spot interruption notice (BidEvictedEvent)
│  → Check if instance had termination protection enabled
│  → Check ASG scaling activity if applicable
│
├─ "Instance is running but application/service is down"
│  → Status checks pass, metrics look normal, but the app doesn't respond
│  → This is application-level, not infrastructure-level
│  → Check CloudWatch Logs for the application (error patterns, crashes, restarts)
│  → Check if the process is running (SSM if available)
│  → Check if the port is listening (security group allows traffic but nothing is bound to the port)
│  → Check memory (CWAgent namespace) — OOM may have killed the process without failing the instance
│  → If load balancer is involved: check target group health and health check path
│  → This may cross into the ECS or EKS skill if the app runs in containers
│
├─ "Is it in an ASG?" / scaling questions
│  → Check instance tags for aws:autoscaling:groupName
│  → If ASG: check ASG desired/min/max, scaling activity, health check type
│  → Check if instance was recently launched or marked unhealthy
│  → Check scaling policies and recent scaling events
│
└─ "What instance type should I use?" / capacity planning
   → Check current utilization metrics over 7d (CPU, memory if CW agent, network)
   → Identify peak vs average usage
   → Compare against instance type limits
   → Recommend right-sizing (up or down)
```

## Key Patterns

### Status Check Interpretation

| Condition | Meaning | Action |
|-----------|---------|--------|
| System status: impaired | Host hardware or hypervisor problem | Stop/start to migrate to new host |
| Instance status: impaired | Guest OS problem (network, disk, kernel) | Check OS logs, possibly reboot |
| Both impaired | Full host failure | Stop/start (new host) |
| System OK, Instance impaired | Software issue only | OS-level troubleshooting |

### Security Group Troubleshooting

When connectivity fails:
1. Get the instance's security groups (ec2:DescribeSecurityGroups)
2. Identify the source and destination (what's trying to reach what)
3. Check inbound rules on the target instance's SG (correct port? correct source CIDR/SG?)
4. Check outbound rules on the source (is outbound allowed?)
5. Check NACL on both subnets (ec2:DescribeNetworkAcls — stateless, need both directions)
6. Check route tables (ec2:DescribeRouteTables — is there a route to the destination?)
7. If VPC peering or Transit Gateway: check routes on both sides

### ENI and IP Issues

Common networking problems beyond security groups:
- **Detached ENI**: Primary ENI detached or secondary ENI removed — instance loses connectivity
- **No available IPs in subnet**: New instances or ENI attachments fail with "InsufficientFreeAddressesInSubnet"
- **Multiple ENIs**: Instance with multiple ENIs may have asymmetric routing — check route tables per ENI
- **Elastic IP disassociated**: Public connectivity lost — check ec2:DescribeAddresses
- **Source/dest check**: Must be disabled for NAT instances or transit — check ec2:DescribeInstanceAttribute

### Memory Metrics

EC2 does NOT publish memory metrics by default. `MemoryUtilization` only appears if:
- CloudWatch Agent is installed and configured (namespace: `CWAgent`)
- Container Insights is enabled (for ECS/EKS on EC2)

If memory metrics are not available, say so. Don't search for them in `AWS/EC2` — they won't be there. Check `CWAgent` namespace with dimension `InstanceId` instead.

### Metrics to Pull for Any EC2 Problem

| Metric | Namespace | What it tells you |
|--------|-----------|-------------------|
| CPUUtilization | AWS/EC2 | Compute pressure |
| NetworkIn / NetworkOut | AWS/EC2 | Network throughput |
| NetworkPacketsIn / NetworkPacketsOut | AWS/EC2 | Packet rate (PPS limits) |
| StatusCheckFailed_System | AWS/EC2 | Host hardware health |
| StatusCheckFailed_Instance | AWS/EC2 | Guest OS health |
| EBSReadOps / EBSWriteOps | AWS/EC2 | Storage IOPS |
| EBSReadBytes / EBSWriteBytes | AWS/EC2 | Storage throughput |
| CPUCreditBalance | AWS/EC2 | Burstable instance credits (T-series) |

### Console Output (ec2:GetConsoleOutput)

Shows the last ~64KB of serial console output. Critical for diagnosing boot failures and OS-level issues. Look for:

| Pattern in output | Meaning |
|-------------------|---------|
| `Kernel panic` | Kernel crash, likely driver or memory issue |
| `Out of memory: Kill process` | OOM killer fired, process killed |
| `No space left on device` | Root volume full |
| `fsck` errors | File system corruption |
| `Failed to start OpenSSH` | SSH won't accept connections |
| `amazon-ssm-agent: failed` | SSM agent not running |
| `cloud-init` errors | User data script failure |
| `RTNETLINK` errors | Network interface misconfiguration |
| `dracut` or `initramfs` errors | Boot volume mount failure |
| `VFS: Unable to mount root fs` | Wrong root device or corrupted AMI |
| `Dependency failed` | /etc/fstab has bad entries — volume missing or syntax error. Instance enters emergency mode |
| `Welcome to emergency mode` | fstab mount failure or kernel issue forced emergency mode. Fix fstab (add `nofail` to secondary mounts) or revert kernel |
| `You are in emergency mode` | Same as above. Check fstab entries for missing volumes or typos |

### Network Bandwidth and PPS Limits (Microbursts)

EC2 instances have network bandwidth and packets-per-second (PPS) quotas. An instance can exceed these limits even when average utilization looks low due to **microbursts** (short spikes lasting milliseconds).

**Key ENA metrics to check** (namespace: `AWS/EC2`, require CW Agent for detailed ENA stats):

| Metric | Meaning |
|--------|---------|
| `bw_in_allowance_exceeded` | Inbound bandwidth throttled — instance hit its Gbps limit |
| `bw_out_allowance_exceeded` | Outbound bandwidth throttled |
| `pps_allowance_exceeded` | Packets per second throttled — too many small packets |
| `conntrack_allowance_exceeded` | Connection tracking table full |
| `linklocal_allowance_exceeded` | Link-local service rate exceeded (DNS, IMDS, NTP) |

**Why average metrics look fine but instance is throttled:**
- CloudWatch metrics are 5-minute averages. A 20-second burst to 10 Gbps averages out to ~0.6 Gbps over 5 minutes.
- CloudWatch can't show microbursts. You need OS-level monitoring at 1-second intervals or ENA driver metrics.

**"Up to" bandwidth instances** (e.g., "up to 10 Gbps"):
- Use network I/O credits to burst. When credits deplete, traffic drops to baseline.
- `bw_*_allowance_exceeded` increases even with available credits (best-effort bursting).

**Investigation steps:**
1. Check `bw_in/out_allowance_exceeded` and `pps_allowance_exceeded` in CloudWatch
2. If increasing: instance is hitting network limits despite low average throughput
3. Compare instance type's published bandwidth to observed peaks
4. Remediation: scale up to a larger instance type (types with "n" like C7gn have higher network), or scale out across multiple instances

### T-Series (Burstable) Instances

If the instance type starts with `t` (t3, t3a, t4g):
- Check CPUCreditBalance — if it hits zero, CPU is throttled to baseline
- Sustained high CPU on a burstable instance means it's burning credits
- Recommend either: switch to unlimited mode, or resize to a non-burstable type

## CloudTrail Events That Matter

| Event | Significance |
|-------|-------------|
| RunInstances | New instance launched |
| StopInstances / StartInstances | Manual or ASG lifecycle |
| RebootInstances | Reboot (check why) |
| TerminateInstances | Instance terminated (ASG health check? Manual?) |
| ModifyInstanceAttribute | Type change, SG change, EBS optimization change |
| AttachVolume / DetachVolume | Storage changes |
| AuthorizeSecurityGroupIngress | Network access opened |
| RevokeSecurityGroupIngress | Network access closed |
| AssociateAddress / DisassociateAddress | Elastic IP changes |

## What to Report

- Instance state and status check results
- Relevant metrics with numbers (current vs baseline)
- Whether the issue is hardware (system status), software (instance status), network (SG/NACL), or capacity (type limits)
- Any recent change that correlates with the problem (with CloudTrail evidence)
- Specific recommended action
- What you couldn't verify (e.g., OS-level logs not accessible via CloudWatch)

For a complete example of a well-structured investigation output, see [examples/investigation-output.md](examples/investigation-output.md).

## Nitro vs Non-Nitro Instances

Some capabilities are only available on Nitro-based instances:

| Feature | Nitro only? | Notes |
|---------|-------------|-------|
| EC2 Serial Console | Yes | Only works on Nitro. Can't use on Xen-based (m4, c4, t2, etc.) |
| ENA network performance metrics | Yes | bw/pps_allowance_exceeded only on ENA (Nitro) |
| EBS-optimized by default | Yes | Older types need explicit EBS optimization flag |
| Instance store NVMe | Yes | Xen instances use paravirtual block devices |

If the instance is not Nitro-based (m4, c4, r4, t2, i3 non-metal, etc.):
- Don't attempt EC2 Serial Console — it won't work
- ENA metrics won't be available — fall back to NetworkIn/Out only
- Say what you can't check and why

## EBS Volume Types and Throttling

When you see `VolumeIOPSExceededCheck` or `VolumeThroughputExceededCheck`, the fix depends on the volume type:

| Volume type | IOPS behavior | Throughput behavior | What to do when throttled |
|-------------|---------------|--------------------|-----------------------------|
| gp2 | Baseline 3 IOPS/GB, burst up to 3000 IOPS (uses I/O credits) | Up to 250 MB/s | If burst credits depleted: wait for refill, resize volume larger (more baseline IOPS), or migrate to gp3 |
| gp3 | Baseline 3000 IOPS (configurable up to 16000) | Baseline 125 MB/s (configurable up to 1000 MB/s) | Increase provisioned IOPS/throughput (no resize needed) |
| io1/io2 | Provisioned (up to 64000 IOPS) | Up to 1000 MB/s | Increase provisioned IOPS |
| st1 | Baseline 40 MB/s per TB, burst 250 MB/s per TB | Uses burst credits | Resize larger for more baseline, or wait for credits |
| sc1 | Baseline 12 MB/s per TB, burst 80 MB/s per TB | Uses burst credits | Same as st1 |

**gp2 burst credit pattern:**
- Small gp2 volumes (< 1TB) rely on burst credits for IOPS above baseline
- Once credits hit zero, IOPS drops to baseline (3 × volume size in GB)
- A 100GB gp2 volume has baseline of only 300 IOPS — easily exhausted
- Check `BurstBalance` metric (AWS/EBS) — if it's at 0%, volume is throttled

**Key insight:** If `VolumeIOPSExceededCheck` is firing on a gp2 volume, the cheapest fix is often just migrating to gp3 (free 3000 IOPS baseline regardless of size).

## Tool Availability

This skill references AWS API calls (ec2:Describe*, ec2:GetConsoleOutput, CloudTrail lookups) that require the AWS API MCP server. If that tool isn't connected:

- You can still investigate using CloudWatch metrics (CPUUtilization, StatusCheckFailed, NetworkIn/Out, EBS metrics)
- Say explicitly what you would check if the API were available
- Don't guess at resource state — state what you know from metrics and what remains unknown
