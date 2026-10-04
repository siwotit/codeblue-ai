# Examples: EKS Investigation Report

Two reference reports showing the expected result format. Each has the same sections: what is happening, evidence, assessment, recommendations, gaps. The numbers are illustrative.

---

# Example 1: Node went NotReady, EBS throttling

## What is happening

Node `ip-10-0-3-41.eu-central-1.compute.internal` (i-0a1b2c3d4e5f60007, prod-cluster, eu-central-1b) went NotReady at 14:20 UTC and recovered at 14:45. Its root EBS volume was throttled from 14:12 to 14:45.

## Evidence

| Metric (13:50 to 14:50 UTC) | During incident | Baseline |
|-----------------------------|-----------------|----------|
| CPUUtilization | 96% | 31% |
| VolumeThroughputExceededCheck (root volume, gp3, 125 MiB/s) | 1 for 33 of 38 minutes | 0 |
| VolumeIOPSExceededCheck (same volume, 3000 IOPS) | 0 | 0 |
| InstanceEBSThroughputExceededCheck | 0 | 0 |
| NetworkIn | 1.8 GB/min at 14:10 | 0.3 GB/min |

| Time (UTC) | Event |
|------------|-------|
| 14:10 | NetworkIn spikes, CPU climbs |
| 14:12 | Root volume throughput throttling starts |
| 14:20 | Node NotReady |
| 14:45 | Throttling clears, node recovers |

Status checks pass, there are no scheduled events, and the console output shows no OOM kills.

## Assessment

**Cause: the root volume hit its gp3 throughput limit (medium-high confidence).** Throttling began 8 minutes before NotReady and ended just before recovery. When the volume is throttled, kubelet and containerd block on disk I/O and miss their heartbeats, so the node is marked NotReady. The high CPU is consistent with I/O wait, not real compute load.

Ruled out: hardware or host failure, the IOPS limit, and the instance-level EBS limits.

## Recommendations

1. Raise the root volume's throughput (gp3 `modify-volume`, for example 250 to 500 MiB/s), and set it in the launch template so new nodes get it too.
2. If image pulls are the trigger, use a larger or dedicated volume for container storage.

## Gaps

| Not checked | Why | How to check |
|-------------|-----|--------------|
| Memory | No CloudWatch agent on the instance, so no memory metrics exist | Install the CloudWatch agent (`CWAgent` namespace) |
| What generated the I/O | Pod and image-pull activity is cluster-side. The network spike then heavy writes fits an image-pull burst | Around 14:10, run `kubectl describe node ip-10-0-3-41.eu-central-1.compute.internal` and `kubectl get events -A --sort-by=.lastTimestamp` |

---

# Example 2: New node never joins, node role missing permissions

## What is happening

Nodegroup `workers-v2` in prod-cluster (eu-central-1) is in `CREATE_FAILED`. Its 3 instances launch and keep running, but never join the cluster.

## Evidence

```
health.issues[0]: code=NodeCreationFailure
  message="Instances failed to join the kubernetes cluster"
  resourceIds=[i-0a1b2c3d4e5f60001, i-0a1b2c3d4e5f60002, i-0a1b2c3d4e5f60003]
```

| Time (UTC) | Event |
|------------|-------|
| 09:02 | 3 instances launched |
| 09:35 | Nodegroup marked `CREATE_FAILED` (`NodeCreationFailure`) |
| 10:20 | All 3 instances still running; the ASG reports them InService and Healthy (automatic node repair is off on this nodegroup, so nothing replaces them) |

| Policy on node role | `workers-v2-node-role` (fails) | `workers-v1-node-role` (works) |
|---------------------|--------------------------------|--------------------------------|
| AmazonEKSWorkerNodePolicy | **Missing** | Attached |
| AmazonEC2ContainerRegistryReadOnly | Attached | Attached |
| AmazonEKS_CNI_Policy | Attached | Attached |

## Assessment

**Cause: the node role is missing `AmazonEKSWorkerNodePolicy` (high confidence).** AWS requires this policy on every node role. It lets the kubelet call the EC2 and EKS APIs it needs to register the node. With it missing, the role does not meet the requirement, so nodes using it cannot join. The working nodegroup `workers-v1` has the policy.

Ruled out: capacity and quota (launches succeed), subnet IPs (212 to 240 free), the network path (public and private endpoint, NAT route present), and authentication (an `EC2_LINUX` access entry exists for the role).

## Recommendations

1. Attach `arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy` to `workers-v2-node-role`.
2. A nodegroup in `CREATE_FAILED` cannot be edited or retried. Delete `workers-v2` (this terminates its 3 instances), then create it again with the corrected role.

## Gaps

| Not checked | Why | How to check |
|-------------|-----|--------------|
| Kubelet logs on the instances | Not visible from AWS APIs. Needed only if the fix does not work | `journalctl -u kubelet` on a node, or ask for the console output (ec2 skill) |
| Control plane `authenticator` logs | Cluster logging has no log types enabled, so there is no history to search | Enable `authenticator` and `api` logging to cover future incidents |
