# What Cargo and Central say to each other

Two conversations, each one way.

**Cargo → Central** is one webhook, where a cluster reports whether it can be used, and the
hourly bucket usage report. The report is signed with the report secret Central hands over at
enrolment. Cargo calls no other Central endpoint.

**Central → Cargo** is bucket work: make a bucket, drop it, add, rotate or remove its keys, and
name the websites that may call it from a browser. Cargo
owns the cluster's admin token, so Central asks rather than acts.

Cargo's side is `cargo/object_storage/api/bucket.py` and the webhook built in
`cargo/object_storage/doctype/object_storage_cluster/object_storage_cluster.py`. Central's
side is `central/api/cargo_webhooks.py` and `central/integrations/cargo_client.py`.

## Central enrolling Cargo

Before Cargo reports anything it has to be told where, and with which secret. Central owns
both, so Central sets them:

```
POST {cargo_url}/api/method/cargo.api.webhooks.configure
X-Cargo-Access-Token: <a token for atlas-cargo:<region-id>>

{"request_url": "https://central.example.com/api/method/central.api.state_delivery.receive",
 "webhook_secret": "a-shared-secret",
 "report_secret": "the-storage-service-secret",
 "enabled": true}
```

`report_secret` is optional. It is the secret on Central's `storage` Service, and Cargo
stores it as `central_report_secret` to sign the usage report. A call without it keeps the
secret already stored.

One receiver and one secret serve every service this region runs, so Cargo stores them on
Cargo Settings and nothing else. Each delivery reads them when it is built, and knows
nothing about how they got there. A repeated call refreshes a rotated secret.

`enabled: false` stops the reports and keeps the receiver, so turning them back on needs no
second handover.

```json
{"request_url": "https://central.example.com/api/method/central.api.state_delivery.receive", "enabled": true}
```

Until this call lands, `central_webhook_url` is empty and a host refuses to configure a
delivery rather than pointing one at a guess. A delivery built before a later call keeps
the old receiver until it is next built.

## Cargo reporting in

A **Frappe Webhook**, created against the cluster when the cluster is created, firing
`on_update` when `doc.status in ("Active", "Failed")` — the two states worth a call.

```
POST {central_url}/api/method/central.api.cargo_webhooks.object_storage_cluster_webhook
X-Frappe-Webhook-Signature: <base64 HMAC-SHA256 of the body>
```

| Send | What it is |
|---|---|
| `region` | Which cluster this is. Central identifies a cluster by its region |
| `region_id` | Atlas's numeric id for that region |
| `service` | `storage` |
| `status` | `Active` once the cluster can hold an object, `Failed` when it never will |
| `service_endpoint` | The cluster's gateway, where benches speak S3 |

Frappe signs the body with the shared `CENTRAL_WEBHOOK_SECRET` rather than sending it, so the
secret never leaves the host and the signature covers the payload. Central looks the region's
Cargo Instance up to find which secret to check — the region in the body selects a secret, it
is never trusted on its own — and every rejection is the same 403, with the real reason in
the Error Log.

Central creates the region's `Service Backend` on the first report, fills in
`service_endpoint`, sets `is_active` from the status, and marks the Cargo Instance
**Registered**. Registration is what mints the instance's access token, so a region that has
never reported has nothing for Central to call it with.

`Active` is not "the nodes joined". It is "this cluster can hold an object", which needs an
applied layout as well — before that Garage answers but its nodes carry no storage role.

## Cargo reporting bucket usage

Cargo reads each bucket's stored bytes from Garage every `usage_sample_minutes` (15 by
default) and keeps the highest reading of each UTC day in `Bucket Usage Day`. A reading
counts for the UTC day it was taken on. A read waits 5 seconds for Garage. A bucket whose
read fails is skipped and logged, and the page goes on. Three reads in a row that time out,
a cluster that cannot be connected to, or an unexpected error ends that page of buckets and
keeps what was read. A database error, such as a lock wait timeout or a deadlock, fails the
job instead, so Frappe rolls it back; chunks committed before it stay. All Error Logs of the sampler for one cluster share one limit of one
an hour, because cluster health already reports the outage. The row names the
bucket, not the record, so a deleted bucket's last day is kept: a bucket is read once more
just before Garage deletes it.

Once an hour Cargo sends every day Central has not confirmed, and every day whose peak rose
after Central confirmed it:

```
POST {central_url}/api/method/central.billing.api.metering.report
X-Central-Service: storage
X-Central-Signature: <base64 HMAC-SHA256 of the raw body, keyed with the report secret>

{"as_of": "2026-11-24T10:15:00Z",
 "lines": [{"subject": "acme-backups", "meter": "bytes_stored", "day": "2026-11-24", "value": 21474836480,
            "evidence": {"v": 1, "peak": 21474836480, "peak_at": "2026-11-24T08:30:00Z", "readings": 42,
                         "first": 21000000000, "first_at": "2026-11-24T00:00:00Z",
                         "last": 21400000000, "last_at": "2026-11-24T10:15:00Z"}}]}
```

| Field | What it is |
|---|---|
| `as_of` | The newest sample in the call, in UTC |
| `subject` | The bucket name |
| `meter` | `bytes_stored` |
| `day` | The UTC date |
| `value` | The day's peak so far, in bytes |
| `evidence` | How the peak was read: the peak and when it was first read, how many readings the day has, and the first and last reading with their times. Bytes are integers and times are UTC. A day first read before Cargo kept readings has no `evidence` |

`evidence` describes the readings up to the call that carries it. A day is sent again only
when its peak rises, so a confirmed day's `readings` and last reading stop at the last send.
Central ignores line keys it does not know, so `evidence` is safe to send to a Central that
does not read it. Cargo ignores answer keys it does not know, such as `underived`.

A call carries at most 1,000 lines. Central answers `{"accepted": n, "rejected": [...]}`.
A day is confirmed only when no rejection could mean it. A rejection is matched by its
`subject`, and by its `day` when it has one. An older Central sends only `subject` and
`reason`, so its rejection holds every day of that bucket in the call. A rejection with no
`subject` holds every day in the call. A day is refused, and
never sent again, only when a rejection names its bucket and day and has `retry` false. Every
other rejected day is sent again next hour. Sending a day twice is harmless: Central keeps the
higher value. Each call's answer is committed before the next call is sent.

When Central cannot be reached or answers 5xx, the days wait for the next hour. A 4xx, or a
200 whose body is not `{"message": {"accepted": ..., "rejected": [...]}}`, stops the run,
settles nothing in that call and leaves an Error Log. Nothing is sent while
`pause_usage_report` is set, or while Cargo has no `central_url` or report secret. Days are
kept for 30 days, sent or not, so Central can be down that long without losing usage.

Datum gets two numbers each hour. `cargo_bucket_usage_outbox_depth` counts the days Central
has never answered for: days not sent yet, and days whose peak rose after the last send.
`cargo_bucket_usage_oldest_unconfirmed_age_days` is the age in days of the oldest of them.
A day Central answered with `retry` true is not counted, so the numbers grow only while
Cargo cannot get an answer from Central, not while Central asks for a day again.

## Central calling Cargo

```
POST {cargo_url}/api/method/cargo.object_storage.api.bucket.<name>
X-Cargo-Access-Token: <the Cargo Instance's cargo_access_token>
```

JSON in, JSON out, unwrapped from Frappe's `{"message": ...}`. Every call takes the same two
fields: `name`, the bucket, and `region`.

Cargo verifies the token against the merged key set at its configured `JWKS_URL`, and holds no
verification secret of its own. Every check below must pass, in this order:

| Check | Rule |
|---|---|
| Algorithm | The header `alg` is `EdDSA`, and so is the algorithm of the key it resolves to. Checked before the key set is fetched. |
| Key ID | `kid` starts with `central:` or `atlas:<region-id>:`, and names a key on the set. |
| Issuer | `iss` equals the issuer that `kid` namespace belongs to. |
| Audience | `aud` is exactly `atlas-cargo:<region-id>`. |
| Claims | `iss`, `sub`, `aud`, `iat` and `exp` are all present, and `exp` is in the future. |

The key set carries both planes' keys, so a valid signature alone does not say who signed. The
key ID decides which issuer a token may claim to be, and `iss` is held to it -- otherwise Central
could sign a token claiming to be Atlas, or the reverse.

A failed check answers the same way whatever failed: `AuthenticationError`, no detail.

Both planes mint this audience. Atlas signs its own bucket call with subject `atlas`, and
Central signs every bucket call with subject `central` and scope `bucket:*`. Both tokens last
5 minutes.

It travels in `X-Cargo-Access-Token` rather than `Authorization`, because Frappe rejects an
unrecognised `Authorization` header with a 401 before the endpoint is reached.

A Cargo host serves one region. A call naming another is refused, and so is one arriving
while the region has no single serving cluster — with two, nothing says which one a bucket
belongs on, and guessing is worse than refusing.

### `create_bucket`

The bucket and the first key that opens it:

```json
{
  "name": "acme-backups",
  "region": "blr",
  "credentials": {"access_key": "GK31c2...", "secret_access_key": "b892c0..."}
}
```

The secret is handed back here and nowhere else — Cargo keeps no copy. Central stores it on
the `Service Credential` and hands the endpoint out from the `Service Backend` beside it.

Either both the bucket and its key exist, or neither does: a bucket nobody holds a key to is
unreachable and invisible, so a failure to issue the key takes the bucket with it. A name
already taken is refused before anything is made.

### `delete_bucket`

Drops the bucket and all its keys, and frees the name with them. Garage refuses a bucket that
still holds objects with a 409, which is what stops this ever taking data with it.

A bucket can have more than one key. Each key has full read and write access to the bucket.

### `add_credentials`

One more key for the bucket. The other keys keep working. Returns the same `credentials`
shape as `create_bucket`, once.

### `rotate_credentials`

Takes `access_key`. Replaces that key and returns the new one in the `credentials` shape,
once. The new key is made before the old one is deleted, so a failed rotation does not lock
the bucket.

### `remove_credentials`

Takes `access_key`. Deletes that key and returns `name`, `region` and `access_key`. The
bucket, its objects and its other keys stay. Cargo refuses to remove a bucket's last key.

### `set_allowed_origins`

Takes `origins`, a list. Replaces the websites that may call the bucket from a browser with
presigned URLs, and returns them as Cargo stored them:

```json
{"name": "acme-backups", "region": "blr", "allowed_origins": ["https://files.example.com"]}
```

An origin is `scheme://host` or `scheme://host:port`, with `http` or `https`. Cargo writes it as
a browser sends it, so `HTTPS://Files.Example.com:443/` comes back as
`https://files.example.com`, and a repeat is dropped. A path, a query, a user name or a wildcard
is refused, and so is the whole call: nothing changes. An empty list removes every website.

The Central dashboard's origin is always allowed and never listed. Cargo refuses the call while
the cluster runs a Garage older than v2.3.0, which would ignore the origins.

### `get_allowed_origins`

Returns the same shape as `set_allowed_origins`.

## Who holds which key

Cargo keeps the powerful token. Central never sees one.

The cluster's `rpc_secret`, admin token and metrics token are minted on the host and stay
there. Central's reach is exactly the calls above — it cannot change a layout, read a
node, or touch an object. Object traffic never goes near either of them: a bench speaks S3 to
the gateway directly, so a Cargo host being down stops new buckets, not existing ones.
