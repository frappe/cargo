# Cargo Telemetry Architecture

Use this as a starting map, not timeless truth. Verify each relevant statement in the current code and cite the evidence used.

## Planes and Ownership

| Plane | Main path | Cargo outage effect to verify |
|---|---|---|
| Storage control | Central/Atlas → Cargo → Garage Admin API | New buckets, credentials, quotas, and cluster administration stop. |
| Storage data | Client/bench → Proxy/Garage gateway → Garage nodes | Existing S3 traffic does not pass through Cargo. Gateway and quorum determine availability. |
| Garage telemetry | Cargo scheduler → each Garage `/metrics` → Datum | Scraping and forwarding stop when Cargo or its scheduler stops. Each node is scraped independently. |
| General telemetry | Producers → Proxy → Datum → ClickHouse | Datum is hosted separately from Cargo; trace producer credentials, Proxy, Datum, and ClickHouse before deciding whether ingestion survives. |
| Health reporting | Cargo probes → Cargo records/logs/webhooks → Central | Self-monitoring cannot prove Cargo itself is alive. Look for an external probe and a freshness alert. |

## Current Evidence Map

- Cargo scheduling: `cargo/hooks.py`
- Garage health and local history: `cargo/object_storage/health/live.py`, `docs/health.md`
- Garage metric scrape and Datum delivery: `cargo/object_storage/health/telemetry.py`
- Datum health probe: `cargo/telemetry/health/live.py`
- Datum lifecycle and Central webhook: `cargo/telemetry/doctype/datum_server/datum_server.py`
- Telemetry auto-provisioning: `cargo/telemetry/spawn.py`
- Bucket API and serving-cluster selection: `cargo/object_storage/api/bucket.py`
- Bucket compensation behavior: `cargo/object_storage/doctype/bucket/bucket.py`
- Garage Admin API calls: `cargo/object_storage/client.py`
- Cargo/Central contract: `docs/central-contract.md`
- Object-storage lifecycle: `docs/object-storage.md`
- Atlas Cargo VM ownership: `../atlas/atlas/service/doctype/cargo_server/cargo_server.py`
- Central state receiver: `../central/central/integrations/state_delivery.py`
- Central Cargo enrollment: `../central/central/infrastructure/doctype/region/cargo_connection.py`

## Baseline Behaviors to Re-verify

- Cargo is a control-plane dependency for bucket operations, not the path for existing S3 object traffic.
- Garage health is refreshed every minute through its gateway. Storage health changes are stored locally; verify whether current code sends them to Central or an alert manager.
- Garage metrics are shipped every five minutes. A scrape failure for one node does not prevent attempts for other nodes.
- Datum intentionally drops failed writes rather than buffering them; verify current retry behavior and data-loss expectations.
- Datum health is checked by Cargo. This does not detect Cargo's own death.
- At replication factor 2, the documented effect of losing one storage node can be roughly two-thirds of partitions becoming unwritable. Confirm the deployed replication factor and Garage health response.
- Webhooks report state changes. A silent component that never changes or reports state requires an independent freshness check.

## Boundaries That Commonly Get Confused

- A running VM is not proof that the Cargo web process, workers, scheduler, database, Redis, DNS, TLS, or Proxy route is healthy.
- A successful Datum health probe is not proof that every producer is still sending.
- A successful scrape is not proof that ingestion, storage, query, dashboard, and alert delivery all work.
- A logged error is not an alert unless it reaches an owned notification channel.
- A retry is unsafe until idempotency and lost-response behavior are proven.

