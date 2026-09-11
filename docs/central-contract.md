# What Cargo and Central say to each other

Two conversations, each one way.

**Cargo → Central** is one webhook, and nothing else: a cluster reports whether it can be
used. Cargo holds no Central credential and calls no Central endpoint.

**Central → Cargo** is bucket work: make a bucket, drop it, rotate or revoke its key. Cargo
owns the cluster's admin token, so Central asks rather than acts.

Cargo's side is `cargo/object_storage/api/bucket.py` and the webhook built in
`cargo/object_storage/doctype/object_storage_cluster/object_storage_cluster.py`. Central's
side is `central/api/cargo_webhooks.py` and `central/integrations/cargo_client.py`.

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

## Central calling Cargo

```
POST {cargo_url}/api/method/cargo.object_storage.api.bucket.<name>
X-Cargo-Access-Token: <the Cargo Instance's cargo_access_token>
```

JSON in, JSON out, unwrapped from Frappe's `{"message": ...}`. Every call takes the same two
fields: `name`, the bucket, and `region`.

The token is a JWT Central signs and Cargo verifies against the key set at the host's
configured `JWKS_URL` — Cargo holds no secret for this, only Central's public keys. Its audience is `central-<region id>-bucket`, minted once per Cargo Instance
when the host registers. Cargo also accepts `atlas-<region id>-admin`, the audience Atlas
checks, because a region's control plane is one trust tier. Both name the region, so a token
lifted from another region's traffic opens nothing.

It travels in `X-Cargo-Access-Token` rather than `Authorization`, because Frappe rejects an
unrecognised `Authorization` header with a 401 before the endpoint is reached.

A Cargo host serves one region. A call naming another is refused, and so is one arriving
while the region has no single serving cluster — with two, nothing says which one a bucket
belongs on, and guessing is worse than refusing.

### `create_bucket`

The bucket and the one key that opens it:

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

Drops the bucket and its key, and frees the name with them. Garage refuses a bucket that
still holds objects with a 409, which is what stops this ever taking data with it.

### `rotate_credentials`

A new key for the bucket, and the end of the one it replaces. Returns the same `credentials`
shape as `create_bucket`, once. The new key is minted before the old one goes, so a rotation
that fails halfway leaves the bucket reachable rather than shut.

### `revoke_credentials`

Takes the bucket's key out of service. The bucket and its objects stay, so this is how a
credential is withdrawn without destroying anything.

## Who holds which key

Cargo keeps the powerful token. Central never sees one.

The cluster's `rpc_secret`, admin token and metrics token are minted on the host and stay
there. Central's reach is exactly the four calls above — it cannot change a layout, read a
node, or touch an object. Object traffic never goes near either of them: a bench speaks S3 to
the gateway directly, so a Cargo host being down stops new buckets, not existing ones.
