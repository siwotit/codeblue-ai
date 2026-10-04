---
name: eks
description: Investigate EKS problems at the AWS layer. Use when nodes fail to join or get terminated, nodegroups report health issues, capacity/quota errors block scaling, a cluster or upgrade is unhealthy, authentication/access entries fail, or the VPC CNI addon is unhealthy. Works via AWS APIs — no kubeconfig required.
---

# EKS Investigation (AWS Layer)

Prerequisites: Read-only AWS credentials for the account owning the cluster. `kubectl` access is NOT required — this skill works entirely from AWS APIs (EKS, EC2, Auto Scaling, CloudTrail, CloudWatch). Where cluster access would add evidence, the skill says so explicitly.

Scope: what AWS APIs can see: nodes/nodegroups, control plane, access/IAM, managed addons.

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

For any node that is missing or NotReady, ask first: **did this node ever become Ready?**
- Never (new node, or a new nodegroup): "New node never joins". It is a bootstrap problem.
- Yes, then it went NotReady: "Existing node goes NotReady". Something degraded or changed.

```
Problem received → What type?
│
├─ "New node never joins" / nodegroup Degraded / NodeCreationFailure (it was never Ready)
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
│         (if vpc-cni has its own IRSA or Pod Identity role, check that role instead: describe-addon vpc-cni)
│    2. Network: node can't reach the API server
│       - Private-only endpoint? Node subnet needs route to it (and cluster SG must allow 443 from node SG)
│       - No NAT/IGW and no VPC endpoints → can't pull images or reach EKS/ECR/S3/EC2 APIs
│       - aws ec2 describe-subnets --subnet-ids <ids> (check AvailableIpAddressCount — exhaustion blocks ENIs)
│    3. AMI/version mismatch: node AMI more than 2 minor versions off control plane, or custom AMI missing bootstrap
│    4. Launch template: bad user data (missing/duplicated bootstrap), wrong SG, IMDSv2 hop limit 1 blocking containers
│  → Do the instances that fail to join get replaced? It depends on automatic node repair:
│    - describe-nodegroup → nodeRepairConfig.enabled. If true, EKS replaces managed nodegroup instances that fail
│      to join, so expect launch-and-replace cycles. If false or absent, the instances keep running: the ASG health
│      check is EC2-based, and a booted instance passes it even if it never joins Kubernetes. Expect them
│      InService and Healthy in describe-auto-scaling-groups while the nodegroup is CREATE_FAILED or Degraded.
│    - Replaced anyway? Find who terminates them: CloudTrail TerminateInstances (node auto repair, Karpenter
│      deleting nodes that do not register in time, automation or a person).
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
│    - Terminated within seconds of launch (pending → terminated): check the stop reason first.
│      describe-instances → StateReason / StateTransitionReason, and the scaling activity StatusMessage
│      (e.g. "Client.InternalError: Client error on launch"). The usual cause is the KMS key behind an encrypted EBS volume:
│      - kms describe-key: KeyState must be Enabled (Disabled or PendingDeletion kills every launch)
│      - The key policy or grants must allow the launcher: the ASG service-linked role
│        (AWSServiceRoleForAutoScaling) for ASG and managed nodegroups, or the Karpenter controller role.
│        Needed actions include kms:CreateGrant, kms:GenerateDataKeyWithoutPlaintext, kms:Decrypt, kms:ReEncrypt*,
│        kms:DescribeKey. Check kms get-key-policy and kms list-grants.
│      - Which key is used: the launch template's EBS KmsKeyId, else the account default
│        (ec2 get-ebs-default-kms-key-id), else the key on the AMI's snapshots. A key in another account also needs
│        a key policy there.
│      - CloudTrail: for ASG and Karpenter launches, look at the KMS calls:
│        LookupEvents with EventSource=kms.amazonaws.com around the launch time, and read errorCode
│        (AccessDenied, KMSInvalidStateException) and the caller (EC2 or the Auto Scaling service role).
│        A direct RunInstances call can return the KMS error synchronously, so check its errorCode too.
│        Also look for recent ScheduleKeyDeletion, DisableKey, PutKeyPolicy or RevokeGrant on that key.
│    - EKS automatic node repair (nodeRepairConfig.enabled on the nodegroup; Karpenter has a NodeRepair feature gate):
│      replaces nodes whose Ready condition is bad for 30 minutes, nodes that fail to join, and manually deleted
│      nodes. It does not react to DiskPressure, MemoryPressure or PIDPressure. By default it pauses when a nodegroup
│      of more than 5 nodes has over 20% unhealthy. Confirm with describe-nodegroup → nodeRepairConfig and the
│      CloudTrail TerminateInstances caller.
│
├─ "Existing node goes NotReady" (it was Ready before; AWS-side evidence only, node conditions need cluster access)
│  0. If the node has been NotReady 30 minutes or more and automatic node repair is enabled
│     (describe-nodegroup → nodeRepairConfig.enabled), EKS may already have replaced it. The instance can be gone.
│  1. Ask the engineer first: are all nodes affected, or one particular node? This decides where to look.
│     - All nodes: the cause is probably shared.
│       - Find the instances from the cluster or nodegroup:
│         - Managed nodegroup: describe-nodegroup → resources.autoScalingGroups[].name, then
│           describe-auto-scaling-groups → Instances[] (InstanceId, LifecycleState, HealthStatus).
│           Or describe-instances with tags eks:cluster-name and eks:nodegroup-name.
│         - Karpenter: tag karpenter.sh/nodepool. Self-managed: tag kubernetes.io/cluster/<cluster>.
│       - Check what they have in common. First, what changed just before the first NotReady:
│         - list-updates (cluster and nodegroup updates running or just finished) and
│           describe-launch-template-versions (a new default version means a new AMI or user data)
│         - CloudTrail, the hour before: UpdateNodegroupVersion, UpdateClusterVersion, UpdateClusterConfig,
│           DeleteAccessEntry, ModifyLaunchTemplate
│       - Then whether the nodes can still reach the API server and be authenticated:
│         - describe-cluster: status ACTIVE rules out an update in progress or a failed cluster. UPDATING or
│           FAILED explains the symptom. ACTIVE does not prove the nodes can reach the API server, so continue
│           with the checks below.
│         - describe-cluster → resourcesVpcConfig: endpointPublicAccess, endpointPrivateAccess, publicAccessCidrs.
│           Private-only needs a route from the node subnets and the cluster security group allowing 443 from
│           the node security group. Public-only needs the nodes' egress path (NAT or IGW) to be intact.
│         - describe-addon vpc-cni: status and health.issues, and whether its role (or the node role) still has
│           the CNI policy and a valid trust. A CNI without EC2 permissions cannot assign IPs, so nodes go NotReady.
│         - list-access-entries: the node role must have an EC2_LINUX entry (or an aws-auth mapping on older
│           clusters). A missing entry means the API server rejects the kubelets.
│         - Control plane logs, only if describe-cluster → logging.clusterLogging has them enabled:
│           authenticator logs show a rejected node role, api logs show errors. Use the cloudwatch skill on
│           /aws/eks/<cluster>/cluster. If logging is off, say so.
│       - Then go deep on one instance as a representative (steps 2 to 4).
│     - One particular node: ask which one and go straight to it.
│       - A node name is usually the private DNS name (e.g. ip-10-0-1-5.eu-central-1.compute.internal):
│         describe-instances with filter private-dns-name=<node name>. An instance ID can be used directly.
│       - Go deep on that instance (steps 2 to 4), and note how it differs from the others (AZ, instance type,
│         AMI, age).
│     - The engineer doesn't know: find the instances as above, run steps 2 and 3 across them and look for
│       outliers. AWS cannot show which nodes Kubernetes reports as NotReady, so if none stands out, ask for
│       kubectl get nodes output.
│  2. Instance health: status checks (system vs instance), scheduled events, stop/reboot, console output
│     (OOM kills, I/O errors, kubelet/containerd failures). The ec2 skill has the detail.
│  3. Resource starvation (the usual cause when CPU or memory is high). Pull CloudWatch for the window around NotReady:
│     - CPUUtilization sustained near 100%; CPUCreditBalance at 0 on t-series (throttled to baseline)
│     - EBS throttling, the frequent hidden culprit. Check all of:
│       - per volume (AWS/EBS): VolumeIOPSExceededCheck, VolumeThroughputExceededCheck, BurstBalance (gp2)
│       - per instance (AWS/EC2): InstanceEBSIOPSExceededCheck, InstanceEBSThroughputExceededCheck
│       - Mechanism: kubelet/containerd block on disk I/O (image pulls, logs, container writes on the root volume),
│         miss node heartbeats, and the node goes NotReady while CPU looks high from I/O wait.
│     - Network allowance exceeded (ENA metrics, only if the CloudWatch agent collects them)
│     - Memory: EC2 publishes none by default. Only present if the CloudWatch agent is installed (CWAgent namespace).
│       If absent, say memory could not be checked.
│  4. Line up the timeline: NotReady time vs metric spikes vs status checks. A throttle or credit exhaustion that
│     starts just before NotReady is strong evidence; one that starts after is not.
│  What this layer cannot see without cluster access: kubelet logs, node conditions
│  (MemoryPressure/DiskPressure/PIDPressure), taints. If the AWS side is clean or inconclusive, give the engineer:
│  kubectl describe node <node>, and read the Conditions and Events sections.
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
│  → Addon role (an addon such as vpc-cni gets AWS AccessDenied). Addons get AWS permissions from
│    the node role, or from an IRSA role or Pod Identity association for their service account:
│    - aws eks describe-addon → serviceAccountRoleArn (IRSA) and podIdentityAssociations
│    - aws eks list-pod-identity-associations --cluster-name <cluster>
│    - aws iam get-role → trust policy must match the OIDC provider (aws iam list-open-id-connect-providers)
│      and the service account subject, or the pods.eks.amazonaws.com principal
│    - aws iam list-attached-role-policies → the addon's managed policy is attached (e.g. AmazonEKS_CNI_Policy)
│  → CloudTrail for the denied call shows which principal was actually used
│  → Authenticator control plane logs (if enabled, see above) show the rejected ARN
│
└─ "VPC CNI problem" / nodes NotReady with "cni plugin not initialized" / pods can't get IPs
   → Why it matters: the VPC CNI (aws-node) attaches ENIs and assigns IPs. If it is unhealthy, the node stays NotReady.
   → aws eks describe-addon --addon-name vpc-cni → status, health.issues[], addonVersion, serviceAccountRoleArn
   → Permissions: the CNI needs EC2 permissions (AmazonEKS_CNI_Policy actions). They come from the addon's IRSA role
     or Pod Identity association, or from the node role if the addon has none. Check the role as in "Addon role" above.
     CloudTrail shows the failure directly: CreateNetworkInterface, AttachNetworkInterface or
     AssignPrivateIpAddresses with errorCode UnauthorizedOperation, and the principal that was denied.
   → IP exhaustion: aws ec2 describe-subnets → AvailableIpAddressCount for the node subnets. Also look for
     InsufficientFreeAddressesInSubnet in CloudTrail errors.
   → What this layer cannot see: aws-node pod restarts and logs. Give the engineer:
     kubectl -n kube-system get pods -l k8s-app=aws-node -o wide
     kubectl -n kube-system logs -l k8s-app=aws-node --tail=100
   → The CNI's own log files are on the node (Amazon Linux; read them over SSM Session Manager or SSH):
     sudo tail -n 200 /var/log/aws-routed-eni/ipamd.log    (ENI and IP allocation; look for UnauthorizedOperation,
                                                            no free IPs, failed ENI attach)
     sudo tail -n 200 /var/log/aws-routed-eni/plugin.log   (pod network setup by the CNI plugin)
```

## Health Issue Codes (describe-nodegroup)

| Code | Meaning | Where to look next |
|------|---------|-------------------|
| NodeCreationFailure | Instances launched but never joined | IAM access entry, network path to API server, console output |
| IamNodeRoleNotFound / IamInstanceProfileNotFound | Node role/profile deleted or inaccessible | IAM: recreate or fix the role, check it wasn't deleted in CloudTrail |
| Ec2LaunchTemplateVersionMismatch | ASG template version ≠ nodegroup's expected version | Someone edited the template outside EKS; reconcile via nodegroup update |
| Ec2LaunchTemplateNotFound | Launch template deleted | CloudTrail DeleteLaunchTemplate |
| InsufficientFreeAddresses | Subnet out of IPs | describe-subnets; add subnets or free IPs |
| ClusterUnreachable | EKS can't reach its own cluster | Control-plane side == recommend AWS Support case |
| AccessDenied | Nodegroup role can't call required APIs | Service-linked role AWSServiceRoleForAmazonEKSNodegroup, SCPs |
| AutoScalingGroupInvalidConfiguration | ASG config drifted from nodegroup spec | Manual ASG edits == reconcile via EKS |

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
- What you couldn't verify, especially anything requiring cluster access, with the exact kubectl commands to run

For a complete example of a well-structured investigation output, see [examples/investigation-output.md](examples/investigation-output.md).

## Tool Usage

- **aws-mcp** (AWS MCP Server, `run_script`, read-only): primary — EKS (DescribeCluster/Nodegroup/Addon, ListUpdates, ListAccessEntries, ListPodIdentityAssociations), IAM get/list, Auto Scaling (DescribeScalingActivities, DescribeInstanceRefreshes), CloudTrail LookupEvents, Service Quotas, and the EC2 describe calls needed for join diagnosis (instance state, subnets, launch templates, console output)
- **ec2 skill**: hand over for any instance deep-dive (status checks, performance, EBS/credit throttling, boot issues)
- **cloudwatch skill**: hand over for metric numbers, baselines, alarms, and log queries (including control plane logs, only when logging is enabled on the cluster)
- **kubectl**: NOT used by this skill — when cluster-side evidence is needed, give the engineer the commands to run

**Important:** Read-only only. Describe/list/get/lookup calls exclusively. Never terminate, scale, update, or modify anything.
