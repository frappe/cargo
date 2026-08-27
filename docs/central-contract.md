# Central contract for Cargo

What Cargo needs from Central. **Draft — the Central side is not built yet.**
`cargo/object_storage/central_client.py` is written against this.

Cargo asks Central for one thing: the secrets a cluster's machines boot with. Cargo never
invents them, because Central is the side that later has to speak to the cluster.

## Transport

- `POST {central_url}/api/method/central.api.atlas.<fn>`, JSON in and out
- Responses are unwrapped from Frappe's `{"message": ...}` envelope
- `Authorization: Bearer <token>`

Central already signs RS256 tokens and publishes a JWKS (`central/sso.py`,
`central/api/jwks.py`), so the same key material that authenticates Cargo to Atlas
authenticates it here. Central verifies its own signature.

## `garage_tokens`

| Field | Type | Notes |
|---|---|---|
| `region` | string | The cluster's region — its identity as far as Central is concerned |
| `vm_ids` | string[] | The machines that will run it |

Returns all three, every time:

```json
{
  "rpc_secret": "...",
  "admin_token": "...",
  "metrics_token": "..."
}
```

Cargo rejects a response missing any of them rather than booting a half-configured cluster.

**Must be idempotent per region.** Every node of a cluster boots with the same three
secrets — that is what makes them peers. If a retry returned fresh values, a cluster would
split into nodes that cannot authenticate to each other.

> **Open:** Central has `garage_tokens` today, but it is HMAC-authenticated
> (`verify_atlas_webhook`), takes a single `host`, and mints per host rather than per
> region. It needs to move to bearer auth and key on region.

## The credential split

Central holds the **scoped** token, Cargo holds the **unscoped** one.

Once a cluster is configured, Cargo mints Central a Garage admin token limited to the
bucket and key endpoints it actually calls — `CreateBucket`, `AddBucketAlias`,
`GetBucketInfo`, `DeleteBucket`, `CreateKey`, `AllowBucketKey`, `DeleteKey`, `GetKeyInfo`.
The cluster's own `admin_token` (scope `["*"]`) never leaves Cargo.

Verified against Garage v2.3.0: `CreateAdminToken` with a `scope` array returns 200, so
this split is available today. Not yet implemented — it belongs to the configuration step.
