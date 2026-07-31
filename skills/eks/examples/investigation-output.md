# Example: EKS Investigation Output

---

## Engineer's question

> "Pods in the checkout namespace on prod-cluster are in CrashLoopBackOff since this morning. We deployed a new version last night."

## Investigation result

**Cluster: prod-cluster** (eu-central-1, EKS 1.29)
**Namespace: checkout**

### Pod Status

```
NAME                               READY   STATUS             RESTARTS   AGE
checkout-api-6b8f9c4d7-abc12      0/1     CrashLoopBackOff   47         8h
checkout-api-6b8f9c4d7-def34      0/1     CrashLoopBackOff   47         8h
checkout-api-6b8f9c4d7-ghi56      0/1     CrashLoopBackOff   47         8h
checkout-api-5a7e8b3c6-jkl78      1/1     Running            0          3d    ← old ReplicaSet
```

### Pod Events (from describe)

```
Warning  BackOff  2m (x47 over 8h)  kubelet  Back-off restarting failed container
```

### Container Status

- State: Waiting (CrashLoopBackOff)
- Last State: Terminated, Exit Code: 1, Reason: Error
- Restart Count: 47

### Logs (--previous, last crash)

```
2026-07-31T04:12:33Z INFO  Starting checkout-api v3.2.0
2026-07-31T04:12:33Z INFO  Connecting to database...
2026-07-31T04:12:34Z ERROR Failed to connect to database: connection refused to checkout-db.checkout.svc.cluster.local:5432
2026-07-31T04:12:34Z FATAL Cannot start without database connection. Exiting.
```

### Deployment History

```
REVISION  CHANGE-CAUSE
3         image updated to checkout-api:v3.1.0 (3 days ago)
4         image updated to checkout-api:v3.2.0 (8 hours ago)  ← current
```

### Service Check

```bash
$ kubectl get endpoints checkout-db -n checkout
NAME          ENDPOINTS   AGE
checkout-db   <none>      3d
```

The `checkout-db` service has **no endpoints** — no pods are backing it.

```bash
$ kubectl get pods -n checkout -l app=checkout-db
No resources found in checkout namespace.
```

The database pods are gone.

### Findings

1. All new checkout-api pods (v3.2.0) crash with exit code 1 because they can't connect to the database at `checkout-db.checkout.svc.cluster.local:5432`.
2. The `checkout-db` service exists but has zero endpoints — no database pods are running to back it.
3. One old pod (from revision 3, v3.1.0) is still running because the deployment hasn't fully rolled over (the new pods keep crashing, so the old RS keeps one replica alive).
4. This is NOT caused by the v3.2.0 deployment itself — the new version is fine, it just can't reach a database that doesn't exist.

### Assessment

**Root cause: The checkout-db pods were deleted or scaled to zero.** The checkout-api deployment is healthy — it crashes because its dependency (the database) is missing, not because of a code bug.

### Recommendation

1. Investigate why checkout-db pods are gone: `kubectl get events -n checkout --field-selector involvedObject.name=checkout-db`
2. Check if checkout-db is a StatefulSet that was accidentally scaled down or deleted
3. Once the database is restored, the checkout-api pods will recover automatically (Kubernetes will keep restarting them and they'll eventually connect)

### What I couldn't verify

- Whether the database was intentionally removed (would need CloudTrail or git history of whoever applied the change)
- Whether there's data loss from the database being gone (depends on PVC state)
