# Failure-Mode Catalog

Review only applicable items, but do not skip a category merely because its main process is running.

## Cargo

- VM stopped, crashed, isolated, or resource-exhausted
- Proxy/DNS/TLS route broken while the VM remains healthy
- web process, workers, scheduler, database, Redis, or queue unavailable
- scheduler stalled while interactive requests still succeed
- disk full, clock skew, expired secrets, invalid configuration, or failed deployment
- Cargo cannot report its own death; require an external liveness and end-to-end probe

## Garage

- gateway down or unreachable
- one storage node down, multiple nodes down, quorum lost, or partition replicas incomplete
- node alive but disk full, read-only, slow, corrupt, or resyncing
- admin API unavailable while S3 traffic still works, and the reverse
- network partition between gateway, storage nodes, Cargo, or clients
- metrics endpoint/token failure independent of Garage data availability

For every node-loss claim, state the deployed replication factor and distinguish reads, writes, existing objects, new buckets, and administrative operations.

## Datum and ClickHouse

- ingestion endpoint, query endpoint, Proxy route, Datum, or ClickHouse unavailable
- authentication/JWKS/token issuance or rotation failure
- writes accepted but not persisted or queryable
- backpressure, timeout, rejected batch, partial batch, malformed samples, or disk exhaustion
- Cargo cannot scrape/send, while other producers may still send directly
- no buffer/retry causes a permanent telemetry gap

## Atlas and Central

- Cargo VM is down but Atlas still records it as Active
- Central cannot reach Cargo, verify signatures, enqueue work, or persist returned credentials
- webhook is lost, rejected, delayed, duplicated, or delivered out of order
- state-change-only reporting leaves stale “Available” state
- monitoring or alert delivery depends on the failed service

## Silent Telemetry Loss

For each expected producer or series, ask:

- What is its expected interval and maximum acceptable silence?
- Is freshness checked at the source, ingestion, storage, query, and alert layers?
- Does the check distinguish “healthy zero” from “no data”?
- Does cardinality change reveal a missing node, cluster, region, or tenant?
- Is the alert evaluated outside the component being monitored?
- Is there a recovery notification and a way to identify the missing interval?

If no freshness/dead-man mechanism is evidenced, label it **Gap**, even when failed send attempts are logged.

## Bucket Creation Walkthrough

Trace this exact sequence in current code:

1. Central/Atlas reaches Cargo and authenticates.
2. Cargo validates the region and selects one serving cluster.
3. Cargo obtains the per-bucket lock.
4. Garage creates an unnamed bucket.
5. Garage assigns the requested alias.
6. Garage creates a key.
7. Garage grants that key access to the bucket.
8. Cargo persists the bucket and encrypted credential record.
9. Cargo returns the one-time secret.
10. The caller persists the secret and acknowledges success.

At every boundary test:

- Cargo down before the call: no mutation should occur; identify caller timeout and alerting.
- Garage/gateway down before mutation: ensure failure is returned and no Cargo record is committed.
- Failure after bucket creation: verify cleanup and what happens when cleanup also fails.
- Failure after key creation or permission grant: verify both key and bucket cleanup.
- Cargo database failure after Garage succeeds: identify orphan detection and reconciliation.
- Response lost after Cargo commits: the caller may not possess the one-time secret. Test retry behavior, idempotency key support, repair path, and orphan visibility.
- Caller database failure after receiving the secret: determine whether credentials can be safely reissued without leaking the original.
- Concurrent duplicate requests: verify lock scope across processes/hosts and final ownership.

## Decision Test

Call a case **Handled** only when all four exist:

1. Independent detection within a stated time.
2. Notification to a named, owned destination.
3. Bounded automatic recovery or a concrete operator runbook.
4. A verification step proving service and telemetry are restored.

Otherwise use **Partial**, **Gap**, or **Unknown** and say exactly what is missing.

