# Example: EKS Investigation Output (AWS Layer)

---

## Engineer's question

> "Our new nodegroup `workers-v2` on prod-cluster has been stuck creating for 40 minutes. Nodes launch but never show up in the cluster."

## Investigation result

**Cluster: prod-cluster** (eu-central-1, EKS 1.29, status ACTIVE)
**Nodegroup: workers-v2** (status CREATE_FAILED, desired 3)

### Nodegroup health

```
health.issues[0]: code=NodeCreationFailure
  message="Instances failed to join the kubernetes cluster"
  resourceIds=[i-0a1b2c3d4e5f60001, i-0a1b2c3d4e5f60002, i-0a1b2c3d4e5f60003]
```

### Lifecycle timeline (ASG scaling activities)

| Time (UTC) | Event |
|------------|-------|
| 09:02 | 3 instances launched (launch succeeded) |
| 09:17 | ASG health check failed, 3 instances terminated |
| 09:18 | 3 replacement instances launched |
| 09:33 | Terminated again |

Repeated launch and terminate pairs about 15 minutes apart. The instances launch fine but never join, so the ASG keeps replacing them.

### What ruled out what

- **Capacity/quota:** ruled out. Launches succeed, no `InsufficientInstanceCapacity` or vCPU errors.
- **Subnets:** ruled out. 212 to 240 free IPs across the three subnets.
- **Network:** cluster endpoint is public and private; nodes have a NAT route. No evidence of a network problem.
- **IAM:** `list-access-entries` shows no entry for `arn:aws:iam::111122223333:role/workers-v2-node-role`. The cluster's `authenticationMode` is `API`, so `aws-auth` mappings are ignored.

### Root cause (high confidence)

The node role has no access entry, and the cluster is in `API` authentication mode. The kubelet authenticates with the node role, is rejected, and never registers. CloudTrail shows the node role was created at 08:55 and no `CreateAccessEntry` for it exists.

The other nodegroup `workers-v1` works because its role has an `EC2_LINUX` entry created 3 months ago.

### Recommended fix

Create an access entry of type `EC2_LINUX` for `workers-v2-node-role` (or recreate the nodegroup through EKS, which creates it automatically). Then update the nodegroup to retry.

### What I couldn't verify

- Control plane `authenticator` logs would show the exact rejected ARN, but `logging.clusterLogging` shows no log types enabled, so there is no history to search. Consider enabling `authenticator` and `api` for future incidents.
- Kubelet logs on the instances (no cluster or SSH access from this layer). If the fix doesn't resolve it, run `get-console-output` via the ec2 skill, or check `journalctl -u kubelet` on a node.
