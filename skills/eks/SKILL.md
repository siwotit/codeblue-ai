---
name: eks
description: Investigate EKS and Kubernetes cluster problems. Use when the engineer asks about pod crashes, node issues, deployments, scheduling failures, networking, addon health, or any Kubernetes-related question.
---

# EKS / Kubernetes Investigation

Prerequisites: The user must have an authenticated kubeconfig (`kubectl` works in their shell). The agent uses `kubectl` commands directly for cluster inspection.

## Decision Tree

```
Problem received → What type?
│
├─ "Pods are crashing" / CrashLoopBackOff / OOMKilled
│  → kubectl get pods -n <namespace> (identify failing pods)
│  → kubectl describe pod <pod> -n <namespace> (events, conditions, container statuses)
│  → Check container state: Waiting? Terminated? What reason?
│  → kubectl logs <pod> -n <namespace> (current logs)
│  → kubectl logs <pod> -n <namespace> --previous (logs from last crash)
│  → If OOMKilled: check resource limits vs actual usage
│  → If CrashLoopBackOff: check exit code and logs from previous run
│
├─ "Pods stuck Pending" / scheduling failures
│  → kubectl describe pod <pod> (look at Events section for scheduling errors)
│  → Common reasons:
│    - Insufficient CPU/memory (no node with enough resources)
│    - Node selector or affinity rules can't be satisfied
│    - Taints on all nodes that the pod doesn't tolerate
│    - PVC can't be bound (storage class, AZ mismatch)
│  → kubectl get nodes -o wide (check node count and status)
│  → kubectl describe nodes (check Allocatable vs Allocated resources)
│  → Check if cluster autoscaler or Karpenter should be adding nodes
│
├─ "Node issues" / NotReady / node pressure
│  → kubectl get nodes (identify NotReady or SchedulingDisabled nodes)
│  → kubectl describe node <node> (check Conditions section)
│  → Conditions to look for:
│    - Ready=False: kubelet failure, node crashed, network partition
│    - MemoryPressure=True: node running low on memory, will evict pods
│    - DiskPressure=True: node disk full, will evict pods
│    - PIDPressure=True: too many processes
│  → Check underlying EC2 instance (use ec2 skill if needed)
│  → Check if node was cordoned/drained (kubectl get node -o yaml | grep -i taint)
│  → Check EKS managed nodegroup health (eks:DescribeNodegroup via AWS API)
│
├─ "Deployment not rolling out" / stuck rollout
│  → kubectl rollout status deployment/<name> -n <namespace>
│  → kubectl get replicasets -n <namespace> (old vs new RS)
│  → kubectl describe deployment <name> -n <namespace> (events, conditions)
│  → If new pods failing: investigate those pods (see "Pods are crashing" above)
│  → Check deployment strategy (RollingUpdate maxUnavailable/maxSurge)
│  → kubectl rollout history deployment/<name> (what changed between revisions)
│  → Compare old vs new pod template (image, env vars, resources, volumes)
│
├─ "Service/ingress not working" / can't reach the app / ALB issues
│  → kubectl get svc -n <namespace> (check service exists, type, ports)
│  → kubectl get endpoints -n <namespace> (does the service have endpoints?)
│  → No endpoints = no pods match the service selector
│  → kubectl get pods -n <namespace> -l <selector-labels> (check pod labels match)
│  → Check if pods are ready (readiness probe passing)
│  → Check NetworkPolicies that might block traffic
│  →
│  → Port chain validation (trace the full path):
│    - Ingress backend port → must match Service port
│    - Service port → maps to targetPort on the pod
│    - targetPort → must match containerPort in the pod spec
│    - If ANY link mismatches: traffic reaches the service but gets connection refused
│    - kubectl get svc <name> -o yaml (check ports[].port and ports[].targetPort)
│    - kubectl get pod <pod> -o jsonpath='{.spec.containers[*].ports}'
│    - Example mismatch: Ingress → svc:80 → targetPort:8080, but container listens on 3000
│  →
│  → If using AWS Load Balancer Controller:
│    - kubectl get ingress -n <namespace> (check rules, annotations, ADDRESS field)
│    - kubectl get targetgroupbindings -n <namespace> (TGB status and target group ARN)
│    - kubectl describe targetgroupbinding <name> (check conditions, events)
│    - kubectl logs -n kube-system -l app.kubernetes.io/name=aws-load-balancer-controller --tail=100
│    - Check for IngressGroup (shared ALB): annotation `alb.ingress.kubernetes.io/group.name`
│    - If ALB not creating: check LBC logs for "failed to reconcile" errors
│    - If targets unhealthy: check target group health (AWS API), check pod readiness, check SG allows ALB → pod traffic
│    - Common annotations to verify:
│      * alb.ingress.kubernetes.io/scheme (internal vs internet-facing)
│      * alb.ingress.kubernetes.io/target-type (ip vs instance)
│      * alb.ingress.kubernetes.io/listen-ports
│      * alb.ingress.kubernetes.io/certificate-arn (TLS)
│      * alb.ingress.kubernetes.io/healthcheck-path
│  →
│  → If using Gateway API:
│    - kubectl get gateways -A (check gateway status and listeners)
│    - kubectl get httproutes -A (check routes, backends, conditions)
│    - kubectl describe gateway <name> (check Accepted/Programmed conditions)
│    - kubectl describe httproute <name> (check ResolvedRefs, Accepted conditions)
│    - If route not working: check parentRefs points to correct gateway
│    - If backend not reachable: check backendRefs service name and port
│  →
│  → If TLS / cert-manager involved:
│    - kubectl get certificates -n <namespace> (check Ready status)
│    - kubectl describe certificate <name> (conditions, events, renewal status)
│    - kubectl get certificaterequests -n <namespace> (pending requests)
│    - kubectl get orders -n <namespace> (ACME challenge status)
│    - kubectl get challenges -n <namespace> (DNS/HTTP challenge state)
│    - kubectl logs -n cert-manager -l app=cert-manager --tail=100
│    - Common issues:
│      * Certificate stuck NotReady: check Order and Challenge status
│      * DNS01 challenge failing: Route53 permissions, zone ID mismatch
│      * HTTP01 challenge failing: ingress not routing /.well-known/acme-challenge
│      * Certificate expired: check `renewalTime` and cert-manager logs for renewal errors
│
├─ "Addon is degraded" / CoreDNS / VPC CNI / kube-proxy / EBS CSI
│  → eks:DescribeAddon via AWS API (check addon status and health issues)
│  → kubectl get pods -n kube-system -l <addon-label> (are addon pods running?)
│  → kubectl describe pod <addon-pod> -n kube-system (events, restarts)
│  → kubectl logs <addon-pod> -n kube-system (addon-specific errors)
│  → Addon-specific checks:
│    - VPC CNI: kubectl get ds aws-node -n kube-system; check WARM_ENI_TARGET, IP allocation
│    - CoreDNS: kubectl get deploy coredns -n kube-system; check for OOM, restarts
│    - kube-proxy: kubectl get ds kube-proxy -n kube-system
│    - EBS CSI: kubectl get pods -n kube-system -l app=ebs-csi-controller
│
├─ "DNS not resolving" / service discovery failures
│  → kubectl run -it --rm debug --image=busybox -- nslookup kubernetes.default
│  → If DNS fails: CoreDNS is the problem — OR the node is network-throttled
│  → kubectl get pods -n kube-system -l k8s-app=kube-dns (CoreDNS pod status)
│  → kubectl logs -n kube-system -l k8s-app=kube-dns (CoreDNS errors)
│  → Check if CoreDNS pods are OOMKilled or crashlooping
│  → Check CoreDNS configmap (kubectl get configmap coredns -n kube-system -o yaml)
│  → Check if pod's /etc/resolv.conf points to the right ClusterIP
│  →
│  → If CoreDNS is healthy but DNS still fails — suspect node network throttling:
│    - Identify which node the affected pod runs on (kubectl get pod -o wide)
│    - Check EC2 NetworkIn/NetworkOut for that node's instance (CloudWatch)
│    - If CW Agent is installed: check bw_out_allowance_exceeded and pps_allowance_exceeded
│    - If CW Agent NOT installed (common): look for indirect signals:
│      * Multiple pods on the same node all showing DNS timeouts
│      * Node is otherwise healthy (CPU/mem fine via kubectl top node)
│      * Instance type is small or "up to" bandwidth (t3, t3a, m5.large, etc.)
│      * Other symptoms: connection timeouts to external services, slow image pulls
│    - DNS is especially vulnerable to PPS throttling (many small UDP packets)
│    - linklocal_allowance_exceeded specifically throttles DNS to the VPC resolver (169.254.169.253)
│    - Fix: larger instance type, spread pods across more nodes, or use NodeLocal DNSCache
│    - To confirm throttling:
│      1. Check if EKS Container Network Observability is enabled (VPC CNI v1.14+)
│         — collects ENA throttling metrics per-node, works on Bottlerocket
│         — metrics in CloudWatch under ContainerInsights namespace with node_net_* prefix
│      2. Check CloudWatch for CWAgent namespace metrics (bw_out_allowance_exceeded, pps_allowance_exceeded)
│         — requires CloudWatch Observability Add-on or CW Agent DaemonSet with ethtool plugin
│      3. If neither is configured: ask the engineer to run on the node:
│         ethtool -S eth0 | grep allowance_exceeded
│      4. Note: Standard Container Insights (without network observability) does NOT collect ENA throttling counters
│
├─ "What changed?" / cluster was fine before
│  → kubectl get events --sort-by='.lastTimestamp' -A (recent cluster-wide events)
│  → kubectl rollout history for relevant deployments
│  → Check CloudTrail for EKS API calls:
│    - UpdateClusterConfig, UpdateNodegroupConfig
│    - CreateAddon, UpdateAddon, DeleteAddon
│    - CreateNodegroup, DeleteNodegroup
│  → Check if a Helm release was upgraded (helm history <release>)
│  → Compare before/after: what's different in the deployment, configmap, or secret?
│
├─ "PVC won't bind" / storage issues
│  → kubectl get pvc -n <namespace> (check status: Pending, Bound, Lost)
│  → kubectl describe pvc <name> (events showing why it's stuck)
│  → Common causes:
│    - StorageClass doesn't exist or is misconfigured
│    - EBS CSI driver not installed or degraded
│    - AZ mismatch (PV in us-east-1a, pod scheduled in us-east-1b)
│    - Insufficient EBS quota
│  → kubectl get storageclass (check default, provisioner, parameters)
│  → Check EBS CSI driver pods are healthy
│
└─ "Cluster is slow" / general performance
   → kubectl top nodes (CPU/memory usage per node)
   → kubectl top pods -n <namespace> (resource consumption per pod)
   → Check CloudWatch Container Insights metrics (cluster, node, pod level)
   → Check if HPA is maxed out (kubectl get hpa -n <namespace>)
   → Check if nodes are overcommitted (Allocatable vs requests vs actual usage)
   → Look for noisy neighbors (one pod consuming disproportionate resources)
```

## Key Patterns

### Pod Failure Classification

| Status | Reason | Meaning | Action |
|--------|--------|---------|--------|
| Waiting | CrashLoopBackOff | Container keeps crashing and restarting | Check logs --previous, check exit code |
| Waiting | ImagePullBackOff | Can't pull the container image | Check image name/tag, registry auth, network |
| Waiting | ErrImagePull | Same as above, first failure | Same as above |
| Waiting | CreateContainerConfigError | Bad config (missing secret, configmap) | kubectl describe pod — check events |
| Terminated | OOMKilled | Container exceeded memory limit | Increase resources.limits.memory |
| Terminated | Error (exit 1) | Application crashed | Check logs for stack trace |
| Terminated | Completed (exit 0) | Container finished normally | Expected for Jobs, not for long-running pods |

### kubectl Commands Reference

**Pods:**
```bash
kubectl get pods -n <ns>                          # List pods and their status
kubectl describe pod <pod> -n <ns>                # Full details, events, conditions
kubectl logs <pod> -n <ns>                        # Current container logs
kubectl logs <pod> -n <ns> --previous             # Previous container logs (after crash)
kubectl logs <pod> -n <ns> -c <container>         # Specific container in multi-container pod
kubectl get pod <pod> -n <ns> -o yaml             # Full pod spec
```

**Nodes:**
```bash
kubectl get nodes -o wide                         # Node list with IPs, versions
kubectl describe node <node>                      # Conditions, capacity, allocated resources
kubectl top nodes                                 # CPU/memory usage (requires metrics-server)
kubectl get node <node> -o jsonpath='{.spec.taints}'  # Check taints
```

**Deployments:**
```bash
kubectl rollout status deployment/<name> -n <ns>  # Rollout progress
kubectl rollout history deployment/<name> -n <ns> # Revision history
kubectl get rs -n <ns>                            # ReplicaSets (old vs new)
kubectl diff -f <manifest>                        # What would change
```

**Events and debugging:**
```bash
kubectl get events -n <ns> --sort-by='.lastTimestamp'  # Recent events
kubectl get events -A --field-selector reason=FailedScheduling  # Scheduling failures
kubectl get events -A --field-selector type=Warning    # All warnings
```

**Networking:**
```bash
kubectl get svc -n <ns>                           # Services
kubectl get endpoints -n <ns>                     # Service endpoints (which pods back it)
kubectl get ingress -n <ns>                       # Ingress rules
kubectl get networkpolicy -n <ns>                 # Network policies
```

### Node Conditions

| Condition | True means | Impact |
|-----------|-----------|--------|
| Ready=False | Kubelet unhealthy or unreachable | Pods on this node are orphaned |
| MemoryPressure | Memory running low | Kubelet will evict pods (BestEffort first) |
| DiskPressure | Disk running low | Kubelet will evict pods, refuse new ones |
| PIDPressure | Too many processes | Kubelet will refuse new pods |
| NetworkUnavailable | Node networking broken | Pods can't communicate |

### EKS Addons

| Addon | What it does | If degraded |
|-------|-------------|-------------|
| vpc-cni (aws-node) | Assigns IPs to pods from VPC subnets | Pods stuck in ContainerCreating, no IP |
| coredns | Cluster DNS resolution | Service discovery fails, DNS timeouts |
| kube-proxy | iptables/IPVS rules for services | ClusterIP services unreachable |
| aws-ebs-csi-driver | Provisions EBS volumes for PVCs | PVCs stuck Pending, volumes don't attach |
| aws-efs-csi-driver | Provisions EFS volumes | EFS-backed PVCs don't mount |

### AWS Load Balancer Controller (LBC)

The LBC manages ALBs and NLBs based on Ingress and Service resources. Key debugging:

```bash
# Check LBC pods are running
kubectl get pods -n kube-system -l app.kubernetes.io/name=aws-load-balancer-controller

# LBC logs (most errors show here)
kubectl logs -n kube-system -l app.kubernetes.io/name=aws-load-balancer-controller --tail=100

# Check TargetGroupBindings (maps K8s service to AWS target group)
kubectl get targetgroupbindings -A
kubectl describe targetgroupbinding <name> -n <namespace>

# Check Ingress status (ADDRESS should show ALB DNS)
kubectl get ingress -n <namespace>
kubectl describe ingress <name> -n <namespace>
```

**Common LBC problems:**

| Symptom | Cause | Fix |
|---------|-------|-----|
| Ingress has no ADDRESS | LBC can't create ALB — check LBC logs | IAM permissions, subnet tags, or security group issues |
| ALB exists but targets unhealthy | Pod not ready or SG blocks ALB→pod | Check target-type (ip vs instance), verify SG allows ALB traffic |
| 404 from ALB | No matching rule for the request | Check ingress path rules and host matching |
| Mixed HTTP/HTTPS not working | Missing `listen-ports` annotation or certificate-arn | Add `alb.ingress.kubernetes.io/listen-ports` and `certificate-arn` |
| IngressGroup not sharing ALB | group.name annotation mismatch or different scheme | All ingresses in a group must have same scheme (internal/internet-facing) |

### cert-manager / TLS Troubleshooting

```bash
# Full certificate lifecycle check
kubectl get certificates -n <namespace>           # Is it Ready?
kubectl describe certificate <name>               # Conditions, events
kubectl get certificaterequests -n <namespace>    # Request status
kubectl get orders -n <namespace>                 # ACME order status
kubectl get challenges -n <namespace>             # Challenge in progress?

# cert-manager logs
kubectl logs -n cert-manager -l app=cert-manager --tail=100
kubectl logs -n cert-manager -l app=cert-manager-webhook --tail=50
```

**Certificate not becoming Ready — trace the chain:**
```
Certificate → CertificateRequest → Order → Challenge(s)
```
Each level can fail independently. Check from right to left (Challenge first).

**Common cert-manager problems:**

| Symptom | Cause | Fix |
|---------|-------|-----|
| Challenge stuck Pending | DNS01: Route53 IAM permissions or wrong zone ID. HTTP01: ingress not serving /.well-known/acme-challenge | Fix IAM or check LBC is routing ACME paths |
| Order failed | Rate limiting by Let's Encrypt, or invalid domain | Check order events, wait for rate limit reset |
| Certificate expired despite cert-manager running | Renewal failed silently — check cert-manager logs | Fix the underlying issue (usually DNS01 permissions changed) |
| Webhook timeout | cert-manager-webhook pod not ready | Check webhook pod status and network policies |

### VPC CNI Specific Issues

The VPC CNI (aws-node DaemonSet) is the most common source of EKS networking problems:

```bash
kubectl get ds aws-node -n kube-system                    # Is it running on all nodes?
kubectl logs -n kube-system -l k8s-app=aws-node --tail=50 # Recent errors
kubectl get eniconfigs                                     # Custom networking config (if used)
```

**Common VPC CNI problems:**
- **Pods stuck in ContainerCreating with "failed to assign an IP address"**: Subnet has no free IPs, or ENI limit reached on the node
- **Warm pool issues**: Check `WARM_ENI_TARGET` and `WARM_IP_TARGET` env vars on the aws-node DaemonSet
- **Secondary IP exhaustion**: Instance type determines max pods (ENIs × IPs-per-ENI)
- **Custom networking misconfigured**: ENIConfig doesn't match the node's AZ

### Resource Requests and Limits

When pods can't schedule or get OOMKilled:

```bash
# Check what a pod requests vs its limit
kubectl get pod <pod> -n <ns> -o jsonpath='{.spec.containers[*].resources}'

# Check node allocatable vs what's already allocated
kubectl describe node <node> | grep -A 5 "Allocated resources"
```

**Key insight:** If sum of all pod `requests` on a node exceeds node `allocatable`, new pods can't schedule. But if `limits` exceed allocatable, pods can use burst resources until they get OOMKilled.

## CloudTrail Events for EKS

| Event | Significance |
|-------|-------------|
| UpdateClusterConfig | Cluster settings changed (logging, networking, auth) |
| UpdateClusterVersion | Cluster upgrade initiated |
| CreateNodegroup / UpdateNodegroupConfig | Nodegroup changes |
| DeleteNodegroup | Capacity removed |
| CreateAddon / UpdateAddon / DeleteAddon | Addon lifecycle |
| AssociateEncryptionConfig | Encryption settings changed |

## What to Report

- Cluster health (nodes ready, addon status)
- Specific pod/deployment failures with reasons and evidence from events/logs
- Whether the issue is scheduling (capacity), application (crash), networking (DNS/CNI), or configuration (bad manifest)
- What changed recently (rollout, addon update, nodegroup change)
- Recommended fix (specific: "increase memory limit to X", "add toleration for taint Y", "scale nodegroup to Z nodes")
- What you couldn't verify

For a complete example of a well-structured investigation output, see [examples/investigation-output.md](examples/investigation-output.md).

## Tool Usage

- **kubectl** (via shell): Primary investigation tool for cluster state, pods, nodes, events, logs
- **awslabs.aws-api-mcp-server**: EKS control plane (DescribeCluster, DescribeAddon, DescribeNodegroup)
- **awslabs.cloudwatch-mcp-server**: Container Insights metrics, log groups for EKS

**Important:** Only use read-only kubectl commands (get, describe, logs, top). Never use apply, delete, patch, scale, edit, drain, cordon, taint, or exec.
