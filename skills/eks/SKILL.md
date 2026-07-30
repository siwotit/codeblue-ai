---
name: eks
description: Investigate an EKS or Kubernetes cluster problem. Use when the engineer reports pod crashes, node issues, addon failures, scheduling problems, or EKS-related alarms.
---

## Investigation Steps

1. Identify the cluster, namespace, and workload from the engineer's input
2. Check cluster status — is the cluster itself healthy?
3. Check node conditions — any NotReady, MemoryPressure, DiskPressure, PIDPressure?
4. List/describe pods — look for CrashLoopBackOff, OOMKilled, ImagePullBackOff, Pending
5. Check pod events — FailedScheduling, FailedMount, Unhealthy, BackOff
6. Check EKS addons — VPC CNI, CoreDNS, kube-proxy, EBS CSI driver health
7. Look for recent rollouts, scaling events, or config changes
8. Check CloudWatch metrics and Container Insights for the cluster

## Key patterns

- **Pods in CrashLoopBackOff**: Application crash — check logs and exit codes
- **Pods OOMKilled**: Memory limit too low or leak — check resource limits vs actual usage
- **Pods stuck Pending**: Scheduling failure — check node capacity, taints, affinity rules
- **ImagePullBackOff**: Wrong image tag, registry unreachable, or auth expired
- **Nodes NotReady**: Kubelet failure, network partition, or instance terminated
- **VPC CNI degraded**: Pods can't get IPs — ENI limits, subnet exhaustion
- **CoreDNS degraded**: DNS resolution failures across the cluster

## What to report

- Cluster and node health status
- Specific pod failures with reasons and affected namespaces
- Whether an addon degradation is causing downstream failures
- Whether a recent deployment/rollout triggered the issue
- Recommended fix (rollback deployment, scale nodes, fix addon, adjust resource limits)
- What you couldn't check and what remains uncertain
