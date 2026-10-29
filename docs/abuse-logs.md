# Abuse logs and metrics

Status: planned. Cargo does not implement this yet.

## Purpose

Each region keeps its NAT log, SNI log and proxy access log for 180 days. These logs let Central find which VM used an address at a given time, and match traffic against abuse feeds. Cargo keeps them in one log bucket on the regional Garage cluster ([object storage](object-storage.md)).

Each region also keeps per-VM abuse metrics for 30 days on its Datum Server. Central reads them to detect abuse.

Cargo also checks public buckets against phishing feeds and suspends a public bucket on request from Central, if Cargo offers public buckets.

Other documents:

- Log formats: [Atlas NAT log](https://github.com/frappe/atlas/blob/develop/docs/abuse/metrics.md#nat-log) and [Atlas SNI log](https://github.com/frappe/atlas/blob/develop/docs/abuse/metrics.md#sni-log).
- Uploads from Metal hosts and proxy nodes: [Atlas log upload](https://github.com/frappe/atlas/blob/develop/docs/abuse/contract.md#log-upload).
- Per-VM counters: [Atlas counters](https://github.com/frappe/atlas/blob/develop/docs/abuse/metrics.md#counters) and [Atlas telemetry](https://github.com/frappe/atlas/blob/develop/docs/abuse/contract.md#telemetry).
- Detection, cases, policy, and retention of Central records: [Central abuse spec](https://github.com/frappe/central/blob/develop/spec/ABUSE.md).

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
| Read key | `read` | Central, on the Region's Cargo connection. Central reads the bucket for feed matching and report attribution. |

Atlas has no read key. A write key cannot list or read objects. A Metal host cannot read the logs of another host.

Garage has no bucket policies, no per-prefix permissions and no object lock. A write key can replace or delete any object in the bucket. Keep write keys on the writers only. Rotate a key if a writer is compromised.

Cargo stores each secret encrypted, as for other buckets. The handover is manual:

1. An operator copies the endpoint, the bucket name, the host write key and the proxy write key from Cargo into Atlas. Atlas writes each write key into the config of its writers. See [Atlas log upload](https://github.com/frappe/atlas/blob/develop/docs/abuse/contract.md#log-upload).
2. An operator copies the endpoint, the bucket name and the read key from Cargo into the Region's Cargo connection in Central.

To rotate a key:

1. Cargo makes the new key.
2. The operator copies the new key into its holder: Atlas for a write key, Central for the read key. Atlas gives a new write key to the writers.
3. Cargo removes the old key.

The old key works until step 3, so a rotation does not stop uploads or reads.

## Access for investigation

Central reads the bucket with the read key:

- Feed matching: after each 5-minute upload, Central matches the new NAT and SNI objects against its abuse feeds.
- Report attribution: for an abuse report, Central finds the VM that used an address at the reported time.

See [Central feeds](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#feeds).

For an investigation that Central cannot answer, an operator adds a temporary `read` key on the Cargo desk. The operator removes the key when the investigation ends.

SNI rows can be personal data. Readers do not copy whole objects out of the bucket to Datum, Central or a laptop. Central keeps only the evidence that the Central abuse spec defines.

## Failure behavior

| Failure | Effect |
|---|---|
| The gateway does not answer, or a partition cannot be written | Uploads fail. Writers keep up to 7 days of logs locally and alert. Cluster health is Critical ([cluster health](health.md)). |
| The bucket quota is full | Garage refuses uploads. Writers keep logs locally as above. Customer buckets keep their space. |
| The Cargo host is down | Uploads and Central reads continue: writers and Central speak S3 to the gateway directly. Key changes and rule checks wait for the next run. |
| The lifecycle rules are missing or changed | The next run sets them again and writes an Error Log. Until then, objects do not expire. |
| A write key leaks | Rotate the key. Objects that the leaked key replaced or deleted cannot be restored. |

The log bucket does not change cluster health. A full or failed log bucket does not stop customer buckets, and a customer bucket problem does not change the log bucket.

## Per-VM metrics

Metal exports per-VM abuse counters to the regional Datum Server. Their names start with `vm_abuse_`, and the label `virtual_machine` holds the Atlas VM name. Atlas specifies the [counters](https://github.com/frappe/atlas/blob/develop/docs/abuse/metrics.md#counters) and the [export](https://github.com/frappe/atlas/blob/develop/docs/abuse/contract.md#telemetry).

Storage:

| Property | Value |
|---|---|
| Table | A separate ClickHouse table in the `datum` database, for `vm_abuse_*` series only. It has the same columns as `datum.samples`. |
| Retention | 30 days, by a ClickHouse `TTL` on the sample time, with `ttl_only_drop_parts` as on `datum.traces` |
| Other series | Stay in `datum.samples`, with no TTL. Datum keeps them forever. |

Central read user:

| Property | Value |
|---|---|
| ClickHouse user | `central` |
| Grant | `SELECT` on the per-VM table only. No other table, no `INSERT`, no DDL. |
| Made by | The Datum ACL migration (`datum-migrate`), the same way as the `insights` user |
| Password | Made by Cargo on the Datum Server, as for `datum` and `insights`: 32 hex characters, set in `before_save` |
| Endpoint | `https://telemetry-read-svc.<wildcard_domain>`. nginx on the Datum host sends queries and `/ping` to ClickHouse and refuses other paths. |

The `insights` user has `SELECT` on all of `datum.*`, so it can also read the per-VM table.

Cargo changes:

- Datum Server gets the Password field `central_user_password`, in `USER_PASSWORDS`. `before_save` fills it. The "Show Password" button shows it to a System Manager.
- `DatumServer.environment` adds `CENTRAL_USER_PASSWORD`. `install.sh` and `update.sh` require it and give `--central-user-password` to `datum-migrate`.
- A Datum Server that exists before this change gets the user on its next update, because `update.sh` also runs `datum-migrate`.

Handover to Central: an operator copies the endpoint, the user name and the password from the Datum Server into the Region's Cargo connection in Central. This is the same manual handover as the read key of the log bucket.

Cargo does not send the password in a call. The only Datum call from Cargo to Central is the Frappe Webhook for the Datum Server status, and Frappe keeps each webhook body in the Webhook Request Log. The `configure` call from Central sends no data back that could hold it.

If the Datum Server does not answer, Central cannot read per-VM metrics for the region.

Dependencies in [Datum](https://github.com/frappe/datum). Cargo cannot do these parts:

1. A migration that makes the per-VM table with the 30-day TTL.
2. Ingest that writes each series with a name that starts with `vm_abuse_` to the per-VM table, not to `datum.samples`. Datum decides if the per-VM table also feeds `daily_ingestion_stats`.
3. A change to `000_acl.sql` that makes the `central` user with its grant, and a `--central-user-password` argument on `datum-migrate`.
4. A password rotation. `000_acl.sql` uses `CREATE USER IF NOT EXISTS`, so a new password on the Datum Server does not change an existing user. Until Datum sets the password on each run, a rotation needs an operator to run `ALTER USER` on the Datum host.

## Public buckets

This section applies only if Cargo offers public buckets. Cargo has no public buckets today: every key is private, and Cargo does not turn on Garage website access.

Check:

- When a bucket becomes public, and every day after, Cargo checks its public hostname against the phishing feeds. The feed URLs are a Cargo setting, with no default.
- A match is a report to Central (see [reports to Central](#reports-to-central)). Cargo does not suspend a bucket on its own. Central owns the case and the decision.

Suspend:

- Central calls `POST {cargo_url}/api/method/cargo.object_storage.api.bucket.suspend_bucket` with `name`, `region` and `reason_code`. Cargo turns off website access on the bucket.
- The bucket, its objects and its keys stay. This keeps the evidence.
- Central calls `unsuspend_bucket` at the same path, with `name` and `region`. Cargo turns website access on again.
- Both calls are idempotent and return `name` and `region`. They use the same token checks as the other bucket calls ([Central contract](central-contract.md#central-calling-cargo)). Both refuse the log bucket name.

## Reports to Central

The log bucket and the per-VM metrics add no field to the cluster report or the Datum Server report.

A feed match on a public bucket adds an optional `abuse_signals` list to the existing `storage` report to `central.api.state_delivery.receive`. Cargo sends a `storage` report when the check finds a match. The Central spec defines the shape of the list: see [Central reports from Cargo](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#reports-from-cargo) and [Central inbound webhooks](https://github.com/frappe/central/blob/develop/spec/WEBHOOKS.md#cargo-reports).

Each item has `signal_id`, `bucket`, `finding_code` (`Phishing:Bucket/Reported`), `severity`, `evidence`, `window_start` and `window_end`.

## Deploy order

Central owns the [deploy order](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#deploy-order). Cargo is step 1: the log bucket and its keys, the per-VM table with its TTL, and the Central read user.
