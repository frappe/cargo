# Abuse logs and metrics

Status: planned. Cargo does not implement this yet.

## Purpose

Each region keeps its NAT log, SNI log and proxy access log for 180 days. [Watcher](watcher.md) reads them to match traffic against abuse feeds, and to find which VM used an address at a given time. Cargo keeps them in one log bucket on the regional Garage cluster ([object storage](object-storage.md)).

Each region also keeps per-VM abuse metrics for 30 days on its Datum Server. Watcher reads them to detect abuse.

If Cargo offers public buckets, Cargo also suspends a public bucket on request from Central.

Other documents:

- Log formats: [Atlas NAT log](https://github.com/frappe/atlas/blob/develop/docs/abuse/metrics.md#nat-log) and [Atlas SNI log](https://github.com/frappe/atlas/blob/develop/docs/abuse/metrics.md#sni-log).
- Uploads from Metal hosts and proxy nodes: [Atlas log upload](https://github.com/frappe/atlas/blob/develop/docs/abuse/contract.md#log-upload).
- Per-VM counters: [Atlas counters](https://github.com/frappe/atlas/blob/develop/docs/abuse/metrics.md#counters) and [Atlas telemetry](https://github.com/frappe/atlas/blob/develop/docs/abuse/contract.md#telemetry).
- Detection, feeds and findings: [Watcher](watcher.md).
- Cases, policy, and retention of Central records: [Central abuse spec](https://github.com/frappe/central/blob/develop/spec/ABUSE.md).

## Log bucket

The log bucket belongs to tenant `0`. It is infrastructure, not a customer bucket.

| Property | Value |
|---|---|
| Count | One for each region, on the Active cluster |
| Name | A Cargo setting, with no default. The name is reserved. |
| Endpoint | The cluster S3 endpoint, the same `service_endpoint` that benches use |
| Retention | 180 days, by Garage lifecycle rules |
| Quota | `max_size_gib`, set by an operator for 180 days of logs and a margin |
| Location | The region. Cargo never copies an object out of the region. |

Cargo makes the bucket, its keys and its lifecycle rules on a scheduled run when the cluster is Active. The run is idempotent: it makes only what is missing.

Every Central bucket call (`create_bucket`, `delete_bucket`, `add_credentials`, `rotate_credentials`, `remove_credentials`, `get_usage`, `set_quota`) refuses the configured log bucket name. Only Cargo changes the log bucket.

## Writers

| Writer | Log | Gets the endpoint and key from |
|---|---|---|
| Metal host | NAT log, SNI log | Atlas, in the metald host config |
| HTTP proxy node | Proxy access log | Atlas, in the proxy node config |

Each writer compresses each closed 5-minute window with zstd and uploads it as one object. The writer deletes its local file only after a confirmed upload. The Atlas documents above specify the writer behavior and the content of each object.

## Object layout

```text
<log>/<writer>/<YYYY-MM-DD>/<HH>-<MM>.zst
```

| Part | Value |
|---|---|
| `<log>` | `nat`, `sni` or `proxy-access` |
| `<writer>` | The Metal host name or the proxy node name |
| `<YYYY-MM-DD>/<HH>-<MM>` | The start of the 5-minute window, in UTC. `<MM>` is `00`, `05`, `10` and so on to `55`. |

Example: `sni/metal-bom-3/2026-11-02/14-05.zst` holds the SNI log of `metal-bom-3` from 14:05 to 14:10 UTC.

Each writer makes one object for each log every 5 minutes, which is 288 objects each day.

The key is the same for each attempt at one window. A retried upload replaces the object and does not make a second copy.

## Lifecycle rules

Garage supports only the `Expiration` and `AbortIncompleteMultipartUpload` actions. A prefix must be inside `Filter`. Garage rejects the old `Prefix` directly in `Rule`. See [Garage S3 compatibility](https://garagehq.deuxfleurs.fr/documentation/reference-manual/s3-compatibility/).

| Rule ID | `Filter` `Prefix` | `Expiration` `Days` | `AbortIncompleteMultipartUpload` `DaysAfterInitiation` |
|---|---|---|---|
| `nat` | `nat/` | 180 | 1 |
| `sni` | `sni/` | 180 | 1 |
| `proxy-access` | `proxy-access/` | 180 | 1 |

```xml
<LifecycleConfiguration>
  <Rule>
    <ID>nat</ID>
    <Status>Enabled</Status>
    <Filter><Prefix>nat/</Prefix></Filter>
    <Expiration><Days>180</Days></Expiration>
    <AbortIncompleteMultipartUpload><DaysAfterInitiation>1</DaysAfterInitiation></AbortIncompleteMultipartUpload>
  </Rule>
</LifecycleConfiguration>
```

Cargo sets the rules with the S3 call `PutBucketLifecycleConfiguration`. This call needs a key with the Garage `owner` permission, so Cargo keeps one owner key for the bucket. On each run, Cargo reads the rules back with `GetBucketLifecycleConfiguration`. If they differ, Cargo sets them again and writes an Error Log.

A 5-minute object is small, so a writer usually uploads it with one `PutObject`. The multipart rule removes the parts of a multipart upload that a writer started and did not complete.

`Expiration` counts from the upload time. A late upload stays 180 days from its upload, so no window is kept for less than 180 days. Garage applies the rules in a background run once a day, so an object can stay up to one day longer.

An object outside these prefixes does not expire. Writers write only these prefixes.

## Keys

| Key | Garage permission | Holder |
|---|---|---|
| Owner key | `owner` | Cargo only. Sets the lifecycle rules. |
| Host write key | `write` | Metal hosts. Atlas stores it and writes it into the metald host config. |
| Proxy write key | `write` | HTTP proxy nodes. Atlas stores it and writes it into the proxy node config. |
| Read key | `read` | Watcher, in Cargo. Watcher reads the bucket for feed matching and attribution. |

Atlas and Central have no read key. A write key cannot list or read objects. A Metal host cannot read the logs of another host.

Garage has no bucket policies, no per-prefix permissions and no object lock. A write key can replace or delete any object in the bucket. Keep write keys on the writers only. Rotate a key if a writer is compromised.

Cargo stores each secret encrypted, as for other buckets. The read key never leaves Cargo. The handover of the write keys is manual: an operator copies the endpoint, the bucket name, the host write key and the proxy write key from Cargo into Atlas. Atlas writes each write key into the config of its writers. See [Atlas log upload](https://github.com/frappe/atlas/blob/develop/docs/abuse/contract.md#log-upload).

To rotate a write key:

1. Cargo makes the new key.
2. The operator copies the new key into Atlas. Atlas gives it to the writers.
3. Cargo removes the old key.

The old key works until step 3, so a rotation does not stop uploads. To rotate the read key, Cargo makes the new key, Watcher uses it from its next run, and Cargo removes the old key.

## Access for investigation

Watcher reads the bucket with the read key:

- Feed matching: Watcher reads each new NAT and SNI object once, and matches it against its abuse feeds. See [Watcher feeds](watcher.md#feeds).
- Attribution: for an abuse report, Central calls `attribute_address`, and Watcher finds the VM that used a shared address at the reported time. See [Watcher attribution](watcher.md#attribution).

Central has no read key. It gets log data only as the `evidence` of a finding and the answer of `attribute_address`.

For an investigation that Watcher cannot answer, an operator adds a temporary `read` key on the Cargo desk. The operator removes the key when the investigation ends.

SNI rows can be personal data. Readers do not copy whole objects out of the bucket to Datum, Central or a laptop. A finding holds no payloads. See [Watcher findings](watcher.md#findings).

## Failure behavior

| Failure | Effect |
|---|---|
| The gateway does not answer, or a partition cannot be written | Uploads fail. Writers keep up to 7 days of logs locally and alert. Cluster health is Critical ([cluster health](health.md)). |
| The bucket quota is full | Garage refuses uploads. Writers keep logs locally as above. Customer buckets keep their space. |
| The Cargo host is down | Uploads continue: writers speak S3 to the gateway directly. Watcher does not run, so no `abuse` report goes to Central. Central alerts when a region sends no `abuse` report for 10 min. See [Watcher findings](watcher.md#findings). Key changes and rule checks wait for the next run. |
| The lifecycle rules are missing or changed | The next run sets them again and writes an Error Log. Until then, objects do not expire. |
| A write key leaks | Rotate the key. Objects that the leaked key replaced or deleted cannot be restored. |

The log bucket does not change cluster health. A full or failed log bucket does not stop customer buckets, and a customer bucket problem does not change the log bucket.

## Per-VM metrics

Metal exports per-VM abuse counters to the regional Datum Server. Their names start with `vm_abuse_`, and the label `virtual_machine` holds the Atlas VM ID, for example `vm-0a1b2c3d4e`. Atlas specifies the [counters](https://github.com/frappe/atlas/blob/develop/docs/abuse/metrics.md#counters) and the [export](https://github.com/frappe/atlas/blob/develop/docs/abuse/contract.md#telemetry).

Storage:

| Property | Value |
|---|---|
| Table | A separate ClickHouse table in the `datum` database, for `vm_abuse_*` series only. It has the same columns as `datum.samples`. |
| Retention | 30 days, by a ClickHouse `TTL` on the sample time, with `ttl_only_drop_parts` as on `datum.traces` |
| Other series | Stay in `datum.samples`, with no TTL. Datum keeps them forever. |

Watcher read user:

| Property | Value |
|---|---|
| ClickHouse user | `watcher` |
| Grant | `SELECT` on the per-VM table only. No other table, no `INSERT`, no DDL. |
| Made by | The Datum ACL migration (`datum-migrate`), the same way as the `insights` user |
| Password | Made by Cargo on the Datum Server, as for `datum` and `insights`: 32 hex characters, set in `before_save` |
| Endpoint | `https://telemetry-read-svc.<wildcard_domain>`. nginx on the Datum host sends queries and `/ping` to ClickHouse and refuses other paths. |

The `insights` user has `SELECT` on all of `datum.*`, so it can also read the per-VM table.

Cargo changes:

- Datum Server gets the Password field `watcher_user_password`, in `USER_PASSWORDS`. `before_save` fills it. The "Show Password" button shows it to a System Manager.
- `DatumServer.environment` adds `WATCHER_USER_PASSWORD`. `install.sh` and `update.sh` require it and give `--watcher-user-password` to `datum-migrate`.
- A Datum Server that exists before this change gets the user on its next update, because `update.sh` also runs `datum-migrate`.

Watcher reads the password from the Datum Server. The password never leaves Cargo, and no operator hands it over. No report to Central holds it, so it is not in the Webhook Request Log.

If the Datum Server does not answer, Watcher cannot read per-VM metrics for the region. See [Watcher inputs](watcher.md#inputs).

Dependencies in [Datum](https://github.com/frappe/datum). Cargo cannot do these parts:

1. A migration that makes the per-VM table with the 30-day TTL.
2. Ingest that writes each series with a name that starts with `vm_abuse_` to the per-VM table, not to `datum.samples`. Datum decides if the per-VM table also feeds `daily_ingestion_stats`.
3. A change to `000_acl.sql` that makes the `watcher` user with its grant, and a `--watcher-user-password` argument on `datum-migrate`.
4. A password rotation. `000_acl.sql` uses `CREATE USER IF NOT EXISTS`, so a new password on the Datum Server does not change an existing user. Until Datum sets the password on each run, a rotation needs an operator to run `ALTER USER` on the Datum host.

The Datum release with items 1 to 3 (the per-VM table, the ingest routing and the `watcher` user in the ACL) must ship before Metal exports the series. If not, the series go to `datum.samples`, with no TTL. See [Central deploy order](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#deploy-order).

## Public buckets

This section applies only if Cargo offers public buckets. Cargo has no public buckets today: every key is private, and Cargo does not turn on Garage website access.

Check: Watcher checks the hostname of each public bucket against the phishing feeds. A match is a finding to Central. Cargo does not suspend a bucket on its own. Central owns the case and the decision. See [Watcher feeds](watcher.md#feeds).

Suspend:

- Central calls `POST {cargo_url}/api/method/cargo.object_storage.api.bucket.suspend_bucket` with `name`, `region` and `reason_code`. Cargo turns off website access on the bucket.
- The bucket, its objects and its keys stay. This keeps the evidence.
- Central calls `unsuspend_bucket` at the same path, with `name` and `region`. Cargo turns website access on again.
- Both calls are idempotent and return `name` and `region`. They use the same token checks as the other bucket calls ([Central contract](central-contract.md#central-calling-cargo)). Both refuse the log bucket name.

## Reports to Central

The log bucket and the per-VM metrics add no field to the cluster report or the Datum Server report.

Watcher sends its findings, a public bucket match included, in the `abuse` report. See [Watcher findings](watcher.md#findings).

## Deploy order

Central owns the [deploy order](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#deploy-order). Cargo is step 1: the log bucket and its keys, the per-VM table with its TTL, and the `watcher` read user. Watcher is the last step, with every Abuse Threshold in Shadow.
