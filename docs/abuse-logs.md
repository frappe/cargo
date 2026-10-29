# Abuse logs

Status: planned. Cargo does not implement this yet.

## Purpose

Each region keeps its NAT log, SNI log and proxy access log for 180 days. These logs let a responder find which VM used an address at a given time. Cargo keeps them in one log bucket on the regional Garage cluster ([object storage](object-storage.md)).

Cargo also checks public buckets against phishing feeds and suspends a public bucket on request from Central, if Cargo offers public buckets.

Other documents:

- Log formats and the hosts that upload them: [Atlas abuse data](https://github.com/frappe/atlas/blob/develop/docs/abuse/data.md) and [Atlas abuse contract](https://github.com/frappe/atlas/blob/develop/docs/abuse/contract.md).
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
| Metal host | NAT log, SNI log | Atlas, in the metald config |
| HTTP proxy node | Proxy access log | Atlas, in the proxy node config |

Each writer compresses each closed hour with zstd and uploads it as one object. The writer deletes its local file only after a confirmed upload. The Atlas documents above specify the writer behavior and the content of each object.

## Object layout

```text
<log>/<writer>/<YYYY-MM-DD>/<HH>.zst
```

| Part | Value |
|---|---|
| `<log>` | `nat`, `sni` or `proxy-access` |
| `<writer>` | The Metal host name or the proxy node name |
| `<YYYY-MM-DD>/<HH>` | The start of the hour, in UTC |

Example: `sni/metal-bom-3/2026-11-02/14.zst`.

The key is the same for each attempt at one hour. A retried upload replaces the object and does not make a second copy.

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

`Expiration` counts from the upload time. A late upload stays 180 days from its upload, so no hour is kept for less than 180 days. Garage applies the rules in a background run once a day, so an object can stay up to one day longer.

An object outside these prefixes does not expire. Writers write only these prefixes.

## Keys

| Key | Garage permission | Holder |
|---|---|---|
| Owner key | `owner` | Cargo only. Sets the lifecycle rules. |
| Host write key | `write` | Metal hosts. Atlas stores it in Abuse Settings and writes it to the metald config. |
| Proxy write key | `write` | HTTP proxy nodes. Atlas stores it in Abuse Settings and writes it to the proxy node config. |
| Read key | `read` | Atlas, in Abuse Settings |

A write key cannot list or read objects. A Metal host cannot read the logs of another host.

Garage has no bucket policies, no per-prefix permissions and no object lock. A write key can replace or delete any object in the bucket. Keep write keys on the writers only. Rotate a key if a writer is compromised.

Cargo stores each secret encrypted, as for other buckets. An operator copies the endpoint, the bucket name, the read key, the host write key and the proxy write key from Cargo into Atlas Abuse Settings. This handover is manual. Atlas then gives each write key to its writers. See [Atlas abuse data](https://github.com/frappe/atlas/blob/develop/docs/abuse/data.md#logs) and [log upload to Cargo](https://github.com/frappe/atlas/blob/develop/docs/abuse/contract.md#log-upload-to-cargo).

To rotate a key:

1. Cargo makes the new key.
2. The operator copies the new key into Atlas Abuse Settings. Atlas gives it to the writers.
3. Cargo removes the old key.

The old key works until step 3, so a rotation does not stop uploads.

## Access for investigation

Responders read logs through Atlas, with the read key: the attribution API (method `NAT Log`) and the evidence pack of a case. Central does not read the bucket.

For an investigation that Atlas cannot answer, an operator adds a temporary `read` key on the Cargo desk. The operator removes the key when the investigation ends.

SNI rows can be personal data. Do not copy objects to Datum, Central or a laptop.

## Failure behavior

| Failure | Effect |
|---|---|
| The gateway does not answer, or a partition cannot be written | Uploads fail. Writers keep up to 7 days of logs locally and alert. Cluster health is Critical ([cluster health](health.md)). |
| The bucket quota is full | Garage refuses uploads. Writers keep logs locally as above. Customer buckets keep their space. |
| The Cargo host is down | Uploads continue: writers speak S3 to the gateway directly. Key changes and rule checks wait for the next run. |
| The lifecycle rules are missing or changed | The next run sets them again and writes an Error Log. Until then, objects do not expire. |
| A write key leaks | Rotate the key. Objects that the leaked key replaced or deleted cannot be restored. |

The log bucket does not change cluster health. A full or failed log bucket does not stop customer buckets, and a customer bucket problem does not change the log bucket.

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

The log bucket adds no field to the cluster report. Central does not use the log bucket.

A feed match adds an optional `abuse_signals` list to the existing `storage` report to `central.api.state_delivery.receive`. Cargo sends a `storage` report when the check finds a match. The Central spec defines the shape of the list: see [Central abuse interfaces](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#interfaces) and [Central inbound webhooks](https://github.com/frappe/central/blob/develop/spec/WEBHOOKS.md#cargo-reports).

Each item has `signal_id`, `bucket`, `finding_code` (`Phishing:Bucket/Reported`), `severity`, `evidence`, `window_start` and `window_end`.

## Deploy order

The deploy order is in the [Atlas abuse contract](https://github.com/frappe/atlas/blob/develop/docs/abuse/contract.md#deploy-order). The log bucket and its keys must exist before Metal starts uploads.
