---
name: eks
description: Investigate EKS problems at the AWS layer. Use when nodes fail to join or get terminated, nodegroups report health issues, capacity/quota errors block scaling, a cluster or upgrade is unhealthy, authentication/access entries fail, or managed addons are degraded. Works via AWS APIs — no kubeconfig required.
---

# EKS Investigation (AWS Layer)

Prerequisites: Read-only AWS credentials for the account owning the cluster. `kubectl` access is NOT required — this skill works entirely from AWS APIs (EKS, EC2, Auto Scaling, CloudTrail, CloudWatch). Where cluster access would add evidence, the skill says so explicitly.

Scope: what AWS APIs can see — nodes/nodegroups, control plane, access/IAM, managed addons. Pod, workload, ingress, and in-cluster DNS investigation is out of scope for now (the Kubernetes layer comes later).

## Handovers

This skill owns the EKS-specific layer: nodegroup health, join failures, ASG/termination forensics, control plane, access entries, managed addons, and EKS-related CloudTrail. When the trail leaves that layer, hand over instead of duplicating.

**→ `ec2` skill** — anything instance-level.
Hand over when a specific instance needs a deep-dive: status checks, console output,
EBS/CPU/network throttling, boot failures.

**→ `cloudwatch` skill** — anything metrics or logs.
Hand over when you need metric numbers, a baseline comparison ("is this anomalous?"),
alarm state/history, or log queries.

**→ The engineer** — anything requiring kubectl.
This skill never runs kubectl. Give the engineer the exact commands to run
(see "When Cluster Access Is Needed").

**Always pass context on handover:**
- instance ID(s)
- cluster and nodegroup name
- the time window
- the specific question you need answered

## Entry Point: Cluster First

Confirm account, region, and cluster name before anything else. A "cluster not found" may just be the wrong region.

```bash
aws eks describe-cluster --name <cluster>   # status, version, endpoint access, accessConfig.authenticationMode, logging, VPC config
aws eks list-nodegroups --cluster-name <cluster>
aws eks list-addons --cluster-name <cluster>
```

Then identify how the nodes are provided and route to the matching branch below:
- **Managed nodegroup:** `describe-nodegroup` → `.nodegroup.health.issues[]` is the primary signal (code, message, resourceIds).
- **Karpenter or self-managed:** no nodegroup object. Work through the ASG/EC2 branches using the instances' tags (`karpenter.sh/nodepool`, `kubernetes.io/cluster/<name>`).

## Decision Tree

```
Problem received → What type?
│
├─ "Nodes not joining the cluster" / nodegroup Degraded / NodeCreationFailure
│  → describe-nodegroup → read health.issues[].code (see Health Issue Codes table)
│  → Did the instance even launch?
│    - aws autoscaling describe-scaling-activities --auto-scaling-group-name <asg>
│    - Launch failed → capacity/quota/launch-template branch below
│    - Launch succeeded but node never registered → bootstrap/network problem:
│  → Check the instance itself:
│    - aws ec2 describe-instances --instance-ids <id> (state, subnet, SG, IAM profile, AMI)
│    - aws ec2 get-console-output --instance-id <id> (bootstrap errors, kubelet failing to reach API)
│  → The four usual causes, in order of frequency:
│    1. IAM: node role not authorized to join
│       - Access entries: aws eks list-access-entries --cluster-name <cluster>
│         (node role needs type EC2_LINUX/EC2_WINDOWS entry, or aws-auth mapping on older clusters)
│       - Role missing policies: AmazonEKSWorkerNodePolicy, AmazonEC2ContainerRegistryReadOnly, CNI policy
│    2. Network: node can't reach the API server
│       - Private-only endpoint? Node subnet needs route to it (and cluster SG must allow 443 from node SG)
│       - No NAT/IGW and no VPC endpoints → can't pull images or reach EKS/ECR/S3/EC2 APIs
│       - aws ec2 describe-subnets --subnet-ids <ids> (check AvailableIpAddressCount — exhaustion blocks ENIs)
│    3. AMI/version mismatch: node AMI more than 2 minor versions off control plane, or custom AMI missing bootstrap
│    4. Launch template: bad user data (missing/duplicated bootstrap), wrong SG, IMDSv2 hop limit 1 blocking containers
│  → Watch for the recycle loop: node launches → fails to join → ASG health check replaces it (~15 min cycle).
│    Scaling activities showing repeated launch+terminate pairs = joining failure, not termination problem.
│
├─ "Nodes being terminated" / instances disappearing / nodes recycled
│  → Who terminated it? Three sources of truth, check in order:
│    1. ASG activity: aws autoscaling describe-scaling-activities (cause field says health check,
│       scale-in, rebalance, or instance refresh)
│    2. CloudTrail: TerminateInstances event → userIdentity tells you WHO
│       (autoscaling.amazonaws.com = ASG, assumed role with "karpenter" = Karpenter, a human = a human)
│    3. Spot: check instance lifecycle (aws ec2 describe-instances → InstanceLifecycle=spot);
│       CloudTrail BidEvictedEvent / instance state change with Spot interruption; ASG capacity-rebalance setting
│  → Common patterns:
│    - Scale-in by Cluster Autoscaler/Karpenter consolidation: expected, verify it's not too aggressive
│    - ASG AZRebalance terminating healthy nodes: check SuspendedProcesses
│    - Health-check replacement loop: see "nodes not joining" above — the join failure is the root cause
│    - Nodegroup update/instance refresh in progress: aws eks describe-update / describe-instance-refreshes
│    - Spot interruptions clustering in one AZ/instance type: diversify or move critical workloads to on-demand
│
├─ "Nodes NotReady" (AWS-side evidence; confirming kubelet state needs cluster access)
│  → Map node → instance ID (node providerID "aws:///<az>/<instance-id>", or instance tags). Then check, in order:
│  → Scope first: ALL nodes NotReady (control plane/network-wide, or a recent nodegroup update/AMI change —
│    list-updates, launch template versions) or ONE node (instance-local)?
│  → Instance health: status checks (system vs instance), scheduled events, stop/reboot, console output
│    (OOM kills, I/O errors, kubelet/containerd failures) — ec2 skill for the detail.
│  → Resource starvation (the usual cause when CPU/memory is high). Pull CloudWatch for the window around NotReady:
│    - CPUUtilization sustained near 100%; CPUCreditBalance at 0 on t-series (throttled to baseline)
│    - EBS throttling — the frequent hidden culprit. Check ALL of:
│      - per volume (AWS/EBS): VolumeIOPSExceededCheck, VolumeThroughputExceededCheck, BurstBalance (gp2)
│      - per instance (AWS/EC2): InstanceEBSIOPSExceededCheck, InstanceEBSThroughputExceededCheck
│      - Mechanism: kubelet/containerd block on disk I/O (image pulls, logs, container writes on the root volume),
│        miss node heartbeats, and the node goes NotReady while CPU looks high from I/O wait.
│    - Network allowance exceeded (ENA metrics, only if the CloudWatch agent collects them)
│    - Memory: EC2 publishes none by default. Only present if the CloudWatch agent is installed (CWAgent namespace).
│      If absent, say memory could not be checked.
│  → Line the timeline up: NotReady time vs metric spikes vs status checks. A throttle or credit exhaustion that
│    starts just before NotReady is strong evidence; one that starts after is not.
│  → Metric names and thresholds are detailed in the ec2 skill — hand over there for the deep-dive.
│  → What this layer CANNOT see without cluster access: kubelet logs, node conditions
│    (MemoryPressure/DiskPressure/PIDPressure), taints. If AWS-side is clean or inconclusive, hand the engineer:
│    kubectl describe node <node>  — and read the Conditions and Events sections.
│
├─ "Volume problem" / EBS attach failures / volume stuck (AWS-side view only)
│  → aws ec2 describe-volumes --volume-ids <id> → State, Attachments[], AvailabilityZone, Encrypted, KmsKeyId
│  → AZ mismatch: an EBS volume attaches only to instances in its own AZ — compare with the node's AZ
│  → Stuck "attaching"/"busy" with no customer-side cause → see AWS escalation guidance
│  → Encrypted volume: KMS key policy must allow the CSI driver/node role (and ASG service-linked role for launches)
│  → Attachment limit: instance types cap attached volumes/ENIs — compare attachment count to the type's limit
│  → Recent changes: CloudTrail AttachVolume/DetachVolume/ModifyVolume/DeleteVolume
│  → PVC/PV/pod state needs cluster access — hand the engineer:
│    kubectl get pvc,pv -A ; kubectl describe pvc <pvc> -n <ns>
│
├─ "Can't scale up" / InsufficientInstanceCapacity / quota errors
│  → ASG scaling activities show the exact launch error:
│    - InsufficientInstanceCapacity: AWS out of that type in that AZ — not your config.
│      Mitigate: more instance types in the nodegroup, more AZs/subnets, different size
│    - VcpuLimitExceeded / instance quota: aws service-quotas get-service-quota
│      --service-code ec2 --quota-code L-1216C47A (Running On-Demand Standard instances)
│    - MaxSpotInstanceCountExceeded: spot vCPU quota
│    - Client.InternalError or encrypted-AMI launch failures: KMS key policy missing ASG service-linked role grant
│  → Also check: nodegroup already at maxSize (describe-nodegroup → scalingConfig),
│    subnet IP exhaustion (describe-subnets → AvailableIpAddressCount)
│
├─ "Cluster unhealthy" / upgrade stuck or failed / API server errors
│  → aws eks describe-cluster → status (ACTIVE/UPDATING/FAILED), version, endpoint access, health.issues
│  → aws eks list-updates --name <cluster>, then describe-update → status and errors[]
│  → Upgrade blockers: subnets with <5 free IPs, cluster role deleted, KMS key for secrets encryption disabled
│  → Control plane logs (api, audit, authenticator, controllerManager, scheduler):
│    - FIRST check describe-cluster → logging.clusterLogging. Logs exist only for types with enabled=true.
│    - Not enabled → say so. There is no history to search; recommend enabling the needed types and re-checking after it recurs.
│    - Enabled → hand over to the cloudwatch skill, log group /aws/eks/<cluster>/cluster
│      (streams: kube-apiserver-*, authenticator-*, kube-apiserver-audit-*). Note log retention may cut off the window.
│  → API server unreachable with status ACTIVE and no customer change → AWS Support case
│
├─ "Access denied" / can't authenticate / nodes or roles rejected
│  → aws eks describe-cluster → accessConfig.authenticationMode (API, API_AND_CONFIG_MAP, CONFIG_MAP)
│  → aws eks list-access-entries / describe-access-entry / list-associated-access-policies for the principal
│  → CONFIG_MAP mode: aws-auth needs cluster access to read — hand kubectl command to engineer
│  → IRSA / Pod Identity (workload gets AWS AccessDenied):
│    - aws eks list-pod-identity-associations --cluster-name <cluster>
│    - aws iam get-role → trust policy must match the OIDC provider (aws iam list-open-id-connect-providers)
│      and the service account subject, or the pods.eks.amazonaws.com principal
│  → CloudTrail for the denied call shows which principal was actually used
│  → Authenticator control plane logs (if enabled, see above) show the rejected ARN
│
├─ "Addon problem" / addon degraded / CoreDNS, CNI, kube-proxy, EBS CSI issues
│  → aws eks list-addons, describe-addon → status, health.issues[], addonVersion, serviceAccountRoleArn
│  → aws eks describe-addon-versions --addon-name <a> --kubernetes-version <v> (is the version compatible?)
│  → Addon update history: list-updates --addon-name; CloudTrail UpdateAddon
│  → Common: addon role missing/trust broken (IRSA), version skew after control plane upgrade,
│    vpc-cni failing on subnet IP exhaustion (describe-subnets → AvailableIpAddressCount)
│  → Pod-level addon state (restarts, logs) needs kubectl — hand to engineer
│
└─ "What changed?" / nodes were fine before
   → CloudTrail, last 24-48h, filtered to the cluster/nodegroup (see CloudTrail Events table)
   → aws eks list-updates --name <cluster> / describe-update (version upgrades, config changes)
   → Launch template versions: aws ec2 describe-launch-template-versions (new default version = new AMI/user data)
   → New AMI release for the same template version pin ($Latest resolves differently over time)
```

## Health Issue Codes (describe-nodegroup)

| Code | Meaning | Where to look next |
|------|---------|-------------------|
| NodeCreationFailure | Instances launched but never joined | IAM access entry, network path to API server, console output |
| IamNodeRoleNotFound / IamInstanceProfileNotFound | Node role/profile deleted or inaccessible | IAM — recreate or fix the role, check it wasn't deleted in CloudTrail |
| AsgInstanceLaunchFailures | ASG can't launch instances at all | Scaling activities — capacity, quota, launch template, KMS |
| Ec2LaunchTemplateVersionMismatch | ASG template version ≠ nodegroup's expected version | Someone edited the template outside EKS — reconcile via nodegroup update |
| Ec2LaunchTemplateNotFound | Launch template deleted | CloudTrail DeleteLaunchTemplate |
| InsufficientFreeAddresses | Subnet out of IPs | describe-subnets — add subnets or free IPs |
| Ec2SecurityGroupDeletionFailure / NotFound | SG dependency problems | Check SGs referenced by the launch template |
| ClusterUnreachable | EKS can't reach its own cluster | Control-plane side — recommend AWS Support case |
| AccessDenied | Nodegroup role can't call required APIs | Service-linked role AWSServiceRoleForAmazonEKSNodegroup, SCPs |
| AutoScalingGroupInvalidConfiguration | ASG config drifted from nodegroup spec | Manual ASG edits — reconcile via EKS |

## Termination Forensics

```bash
# ASG decisions with reasons (cause field is prose and names the trigger)
aws autoscaling describe-scaling-activities --auto-scaling-group-name <asg> --max-items 20

# Who called TerminateInstances
aws cloudtrail lookup-events --lookup-attributes AttributeKey=EventName,AttributeValue=TerminateInstances \
  --start-time <ISO> --end-time <ISO>

# Spot vs on-demand, and lifecycle state
aws ec2 describe-instances --instance-ids <id> \
  --query 'Reservations[].Instances[].{Lifecycle:InstanceLifecycle,State:State.Name,Reason:StateTransitionReason}'

# In-flight nodegroup updates / instance refreshes
aws eks list-updates --name <cluster>
aws autoscaling describe-instance-refreshes --auto-scaling-group-name <asg>
```

| Terminator (CloudTrail userIdentity) | Meaning |
|--------------------------------------|---------|
| autoscaling.amazonaws.com | ASG: health check, scale-in, rebalance, or instance refresh — read the scaling activity cause |
| Role containing "karpenter" | Karpenter consolidation/expiration/drift — check Karpenter's own events if cluster access exists |
| eks.amazonaws.com / nodegroup role | Managed nodegroup update rolling nodes |
| ec2-spot | Spot interruption |
| A human or CI role | Someone did it — check the session name |

## CloudTrail Events for Node Lifecycle

| Event | Significance |
|-------|-------------|
| CreateNodegroup / DeleteNodegroup | Capacity added/removed |
| UpdateNodegroupConfig / UpdateNodegroupVersion | Scaling config, labels/taints, or AMI version changed |
| UpdateClusterVersion | Control plane upgrade — nodes may now lag in version skew |
| TerminateInstances / StopInstances / RebootInstances | Direct instance action — check userIdentity |
| CreateLaunchTemplateVersion / ModifyLaunchTemplate | New user data/AMI/SG for future nodes |
| DeleteLaunchTemplate / DeleteRole | Dependency deleted out from under the nodegroup |
| UpdateAutoScalingGroup / SuspendProcesses / ResumeProcesses | Manual ASG drift |
| CreateAccessEntry / DeleteAccessEntry / AssociateAccessPolicy | Node or user auth to the cluster granted/revoked |
| UpdateClusterConfig / UpdateClusterVersion | Logging, endpoint access, or version changed |
| CreateAddon / UpdateAddon / DeleteAddon | Managed addon changed |

## When Cluster Access Is Needed

This skill stops at the AWS boundary. Hand these to the engineer when the trail crosses it:

```bash
kubectl get nodes -o wide                  # which nodes K8s actually sees, and their status
kubectl describe node <node>               # Conditions (MemoryPressure/DiskPressure), taints, events
kubectl get events -A --field-selector involvedObject.kind=Node
```

Signals that the problem is on the Kubernetes side (and AWS-side is likely clean): instance running and healthy in EC2 but NotReady in the cluster, join succeeded but node immediately cordoned, pressure conditions with normal EC2 metrics.

## What to Report

- Cluster/nodegroup/addon status and any health issue codes, verbatim
- The lifecycle timeline: launched when, joined or not, terminated when and by whom (with CloudTrail/ASG evidence)
- Whether the failure is auth (IAM/access entries), network (subnets/SGs/endpoints), capacity (AWS or quota), configuration (launch template/AMI), or AWS-side (status checks, ClusterUnreachable)
- What changed recently (CloudTrail, launch template versions, nodegroup updates)
- Recommended fix, specific: "add an EC2_LINUX access entry for role X", "add subnet Y to the nodegroup", "raise quota L-1216C47A to Z vCPUs"
- What you couldn't verify — especially anything requiring cluster access, with the exact kubectl commands to run

For a complete example of a well-structured investigation output, see [examples/investigation-output.md](examples/investigation-output.md).

## Tool Usage

- **aws-mcp** (AWS MCP Server, `run_script`, read-only): primary — EKS (DescribeCluster/Nodegroup/Addon, ListUpdates, ListAccessEntries, ListPodIdentityAssociations), IAM get/list, Auto Scaling (DescribeScalingActivities, DescribeInstanceRefreshes), CloudTrail LookupEvents, Service Quotas, and the EC2 describe calls needed for join diagnosis (instance state, subnets, launch templates, console output)
- **ec2 skill**: hand over for any instance deep-dive (status checks, performance, EBS/credit throttling, boot issues)
- **cloudwatch skill**: hand over for metric numbers, baselines, alarms, and log queries (including control plane logs, only when logging is enabled on the cluster)
- **kubectl**: NOT used by this skill — when cluster-side evidence is needed, give the engineer the commands to run

**Important:** Read-only only. Describe/list/get/lookup calls exclusively. Never terminate, scale, update, or modify anything.
