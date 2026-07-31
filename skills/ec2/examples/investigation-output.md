# Example: EC2 Investigation Output

This is what a good investigation result looks like. Use this as a reference for format and thoroughness.

---

## Engineer's question

> "Instance i-0abc123def456 in eu-central-1 has been unreachable for 20 minutes. Can you check what's going on?"

## Investigation result

**Instance: i-0abc123def456** (m5.xlarge, eu-central-1a, running since 2026-06-15)

### Status Checks
- System status: **OK**
- Instance status: **OK**

### Metrics (last 1h vs 24h baseline)

| Metric | Current (1h avg) | Baseline (24h avg) | Assessment |
|--------|-------------------|--------------------|----|
| CPUUtilization | 2.3% | 18.7% | Abnormally low |
| NetworkIn | 0 bytes | 45 MB/min | **Zero traffic** |
| NetworkOut | 0 bytes | 12 MB/min | **Zero traffic** |
| StatusCheckFailed | 0 | 0 | Passing |

### Findings

1. Instance is running and passing both status checks, so the hardware and OS are healthy.
2. Network traffic dropped to exactly zero at 14:42 UTC. Before that, normal traffic pattern (~45 MB/min inbound).
3. Checked CloudTrail: at 14:41 UTC, user `deploy-bot` called `AuthorizeSecurityGroupIngress` on sg-0xyz789 and then `RevokeSecurityGroupIngress` removing the previous rule. The revoke removed inbound TCP port 443 from 0.0.0.0/0.
4. Current security group sg-0xyz789 inbound rules:
   - TCP 22 from 10.0.0.0/8 (SSH, internal only)
   - TCP 8080 from sg-0loadbalancer (new rule added at 14:41)
   - ~~TCP 443 from 0.0.0.0/0~~ (removed at 14:41)

### Assessment

This is a **security group misconfiguration**, not an instance failure. The `deploy-bot` automation removed the port 443 inbound rule at 14:41 UTC and replaced it with port 8080 from a load balancer SG. If the service still listens on 443 and clients connect directly (not through the LB), they're now blocked.

### Recommendation

- If clients should go through the load balancer: update DNS/clients to point at the ALB, not the instance directly. The new SG rule is correct.
- If direct access on 443 is still needed: re-add the inbound rule for TCP 443.
- Either way: confirm with the `deploy-bot` owner whether this change was intentional.

### What I couldn't verify

- Whether the application on the instance is actually listening on port 8080 (would need SSM or OS-level check)
- Whether the load balancer health checks are passing on the new port
