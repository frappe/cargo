# What Cargo and Central say to each other

Two conversations, each one way.

**Cargo → Central** is two flows. Both post a signed report to `central_webhook_url`:

- The Frappe Webhook of each cluster: the cluster reports whether it can be used. See [Cargo reporting in](#cargo-reporting-in).
- Planned: the Watcher `abuse` report, after every Watcher run. See [Planned: abuse protection](#planned-abuse-protection).

Cargo holds no Central token and calls no other Central endpoint.

**Central → Cargo** is bucket work: make a bucket, drop it, add, rotate or remove its keys. Cargo
owns the cluster's admin token, so Central asks rather than acts. Planned: Central also calls
`attribute_address`.

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
 "enabled": true}
```

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
POST {central_url}/api/method/central.api.state_delivery.receive
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

Atlas mints this audience for its own bucket calls, with subject `atlas` and a 5-minute lifetime. Central mints it for its bucket calls with the scope `bucket:*`.

The bucket calls do not check `scope`. Planned: `attribute_address` also needs `iss` = `central` and `scope` = `abuse:attribute`. Any other token gets `403`. Read [Watcher attribution](watcher.md#attribution).

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

A bucket can have more than one key. The key from `create_bucket` has full read and write access to the bucket.

### `add_credentials`

One more key for the bucket. The other keys keep working. Returns the same `credentials`
shape as `create_bucket`, once.

Planned: an optional `permission`, `write` or `read`. With no `permission`, the key has full read and write access, as today.

| `permission` | Garage permission | The key can |
|---|---|---|
| (none) | `read` and `write` | Read, list, write and delete objects |
| `write` | `write` | Write and delete objects. It cannot read or list them. |
| `read` | `read` | Read and list objects |

Garage `write` includes delete. Atlas uses `write` for the log bucket keys of its Metal hosts and proxy nodes. Read [abuse log keys](abuse-logs.md#keys).

### `rotate_credentials`

Takes `access_key`. Replaces that key and returns the new one in the `credentials` shape,
once. The new key is made before the old one is deleted, so a failed rotation does not lock
the bucket. Planned: the new key gets the same `permission` as the old key.

### `remove_credentials`

Takes `access_key`. Deletes that key and returns `name`, `region` and `access_key`. The
bucket, its objects and its other keys stay. Cargo refuses to remove a bucket's last key.

### Planned: abuse protection

The regional log bucket name is a Cargo setting, with no default. Every bucket call above from a Central token, `create_bucket` included, refuses the configured name. A token from Atlas can only add and remove write keys on it. Read [abuse logs and metrics](abuse-logs.md#log-bucket).

[Watcher](watcher.md), a planned Cargo module, detects abuse in the region. It reads the log bucket with the read key, and the per-VM abuse metrics as the read-only ClickHouse user `watcher`. Both credentials stay in Cargo. Central holds no abuse credential of Cargo.

- Cargo to Central: after every run, also with no new finding, Watcher sends a report with `service` = `abuse`, a `checked_until` object and a `findings` list. It posts to `central_webhook_url` and signs with `central_webhook_secret`, as the cluster webhook does. It sends nothing while `central_webhook_enabled` is off. The report has no `status` and no `service_endpoint`. Read [Watcher findings](watcher.md#findings).
- Central to Cargo: `attribute_address` returns the VM that used a shared host address and port at a given time. Cargo finds it in the `nat-ranges` log. It is an exception to the rule that every call takes `name` and `region`: it takes `address`, `port` and `time`, and it is in `cargo.watcher.api`. Cargo refuses a `time` older than 179 days. It needs a Central token with the scope `abuse:attribute`. Read [Watcher attribution](watcher.md#attribution).
- If Cargo offers public buckets, Central also gets `suspend_bucket` and `unsuspend_bucket`. Read [public buckets](abuse-logs.md#public-buckets).

## Who holds which key

Cargo keeps the powerful token. Central never sees one.

The cluster's `rpc_secret`, admin token and metrics token are minted on the host and stay
there. Central's reach is exactly the calls above — it cannot change a layout, read a
node, or touch an object. Object traffic never goes near either of them: a bench speaks S3 to
the gateway directly, so a Cargo host being down stops new buckets, not existing ones.
