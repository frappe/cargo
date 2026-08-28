# What Cargo needs from Central

**Draft. The Central side isn't built yet.**

Cargo asks Central for one thing: the secrets a cluster's machines need to start up. Cargo
never makes these up itself, because Central is the one that has to talk to the cluster
afterwards. The code is `cargo/object_storage/central_client.py`.

## How the calls work

```
POST {central_url}/api/method/central.api.atlas.<name>
Authorization: Bearer <token>
```

JSON in, JSON out, unwrapped from Frappe's `{"message": ...}`.

Central already signs tokens and publishes its public keys (`central/sso.py`,
`central/api/jwks.py`), so the same token that gets Cargo into Atlas works here. Central is
just checking its own signature.

## Asking for secrets — `garage_tokens`

| Send | What it is |
|---|---|
| `region` | Which cluster this is. Central identifies a cluster by its region |
| `vm_ids` | The machines that will run it |

Send back all three:

```json
{
  "rpc_secret": "...",
  "admin_token": "...",
  "metrics_token": "..."
}
```

If any of them is missing, Cargo refuses the whole reply rather than starting a cluster
that's half configured.

**Answer the same thing every time for a region.** Every machine in a cluster starts with
the identical three secrets — that's what lets them recognise each other as one cluster. If
a retry got fresh values, you'd end up with machines that can't talk to each other, and it
would look like a network problem rather than a secrets problem.

Which secrets get asked for is the *service's* choice, not Central's — Cargo sends the list
it wants. Object storage wants these three
(`cargo/object_storage/credentials.py`). Print or email will want their own.

> **Not built yet.** Central has a `garage_tokens` endpoint already, but it's authenticated
> with a shared secret, takes a single `host`, and makes secrets per host. It needs to move
> to bearer tokens and work per region.

## Who holds which key

Cargo keeps the powerful token. Central gets a limited one.

Once a cluster is running, Cargo asks Garage for a second admin token that can only do the
bucket and key operations Central actually performs — `CreateBucket`, `AddBucketAlias`,
`GetBucketInfo`, `DeleteBucket`, `CreateKey`, `AllowBucketKey`, `DeleteKey`, `GetKeyInfo`.
The cluster's real admin token, which can do anything, never leaves Cargo.

Checked against Garage v2.3.0: asking for a token with a limited `scope` works. Not wired up
yet — it belongs with the configuration step.
