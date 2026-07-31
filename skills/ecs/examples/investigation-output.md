# Example: ECS Investigation Output

---

## Engineer's question

> "Our payment-service in the prod cluster keeps restarting. Tasks are failing every few minutes. Circuit breaker hasn't kicked in yet but it's close."

## Investigation result

**Service: payment-service** (prod-cluster, eu-central-1, Fargate, desired: 4)

### Service State
- Running: 2 / Desired: 4
- Pending: 2 (stuck starting)
- Deployments: 1 (PRIMARY, rolloutState: IN_PROGRESS)
- Circuit breaker: enabled, not yet triggered (failure count 6/10)

### Stopped Tasks (last 30 minutes)

| Task ID | Stop Code | Exit Code | Stopped Reason |
|---------|-----------|-----------|----------------|
| abc123 | EssentialContainerExited | 137 | OutOfMemoryError: Container killed |
| def456 | EssentialContainerExited | 137 | OutOfMemoryError: Container killed |
| ghi789 | EssentialContainerExited | 137 | OutOfMemoryError: Container killed |

### Task Definition (payment-service:47, deployed 2h ago)

Changes from revision 46 → 47:
- `image`: `payment-service:v2.8.1` → `payment-service:v2.9.0`
- `memory`: 512 → 512 (unchanged)
- Everything else identical

### CloudWatch Metrics

| Metric | Current | Before deployment (rev 46) |
|--------|---------|---------------------------|
| MemoryUtilization | 98.7% (then OOM) | 62% average |
| CPUUtilization | 34% | 28% |
| RunningTaskCount | fluctuating 2-4 | stable at 4 |

### Findings

1. All task failures are exit code 137 (OOM killed). The container is exceeding its 512MB memory hard limit.
2. This started exactly when task definition revision 47 deployed (v2.9.0 image) 2 hours ago.
3. Previous revision (v2.8.1) ran at ~62% memory utilization with the same 512MB limit. The new version (v2.9.0) uses significantly more memory.
4. Circuit breaker hasn't rolled back because some tasks do start successfully before eventually running out of memory under load.

### Assessment

**Root cause: v2.9.0 has higher memory requirements than v2.8.1.** The 512MB limit was adequate for the old version but not the new one. This is a deployment-induced regression.

### Recommendation

1. **Immediate fix:** Increase memory limit to 1024MB in the task definition and redeploy. This gives headroom for v2.9.0's higher usage.
2. **Or roll back:** Revert to task definition revision 46 (v2.8.1) if the memory increase isn't acceptable.
3. **Follow up:** Have the dev team investigate why v2.9.0 uses 60% more memory (possible memory leak or new dependency loading more data into memory at startup).

### What I couldn't verify

- Whether the memory increase is a leak (grows over time) or a fixed increase (higher baseline). Would need to watch MemoryUtilization over a longer window after increasing the limit.
- Whether the v2.9.0 release notes mention increased resource requirements.
