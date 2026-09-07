# What Cargo and Central say to each other

Every call runs one way: **Cargo calls Central.** Central never calls Cargo. Cargo's side is
`cargo/central_client.py`. Central's side is `central/api/cargo.py`.

## How the calls work

```
POST {central_url}/api/method/central.api.cargo.<name>
X-Cargo-Token: <central_access_token>
```

JSON in, JSON out, unwrapped from Frappe's `{"message": ...}`.

The token is signed by Central, so Central just verifies its own signature — nothing is
shared between the two. It has to travel in `X-Cargo-Token` rather than `Authorization`,
because Frappe rejects an unrecognised `Authorization` header with a 401 before the endpoint
is reached.

The token's scope must be `cargo:central`. Cargo's Atlas token is signed by the same key, so
the scope check is what stops it being replayed here.

The token also names the Cargo Instance it was minted for. Every call below that takes a
`region` must name that instance's own region — anything else is refused before the endpoint
runs, so a valid host cannot ask about a cluster that is not its own. Details in
[bootstrapping.md](bootstrapping.md#one-host-one-region).

There is one endpoint that does not take this token: `request_control_credentials`, which is
how a host gets it in the first place. See [bootstrapping.md](bootstrapping.md).

## Enrolling — `request_control_credentials`

```
X-Cargo-Bootstrapping-Token: <one-time token>
```

| Send | What it is |
|---|---|
| `base_url` | Where this host is served, so Central has a record of it |

Returns `central_access_token` and `atlas_access_token`, both naming the Cargo Instance the
bootstrapping token was minted for. The bootstrapping token is spent by this call and cannot
be used again, including by a second request arriving at the same moment. Full walkthrough in
[bootstrapping.md](bootstrapping.md).

## Asking for cluster secrets — `garage_tokens`

| Send | What it is |
|---|---|
| `region` | Which cluster this is. Central identifies a cluster by its region, and it must be the caller's own |
| `vm_ids` | The machines that will run it |

Returns all three:

```json
{
  "rpc_secret": "...",
  "admin_token": "...",
  "metrics_token": "..."
}
```

If any is missing, Cargo refuses the whole reply rather than starting a half-configured
cluster.

**The same answer every time for a region.** Every machine in a cluster boots with the
identical three secrets — that is what makes them one cluster rather than three lone nodes.
If a retry got fresh values you would get machines that cannot recognise each other, and it
would look like a network fault rather than a secrets fault. Central generates them once,
per field, and stores them on a `Service Backend` row for the region.

That row starts inactive with no endpoint: Central knows the cluster's secrets before it
knows where the cluster is.

Which secrets get asked for is the *service's* choice, not Central's. Object storage wants
these three (`cargo/object_storage/credentials.py`); print and email will want their own.

## Reporting status — `register_cluster`

| Send | What it is |
|---|---|
| `region` | The cluster |
| `active` | Whether Central may use it |
| `base_url` | Garage's admin API, where Central manages buckets and keys |
| `s3_endpoint` | Where benches read and write objects |
| `web_endpoint` | Where buckets are served as static sites |

Every endpoint is built from the cluster's gateway address — every S3 and admin call goes
through the gateway — and none of them is behind TLS.

Central fills the three endpoints in on the `Service Backend` row and marks it active. Until
this lands the backend has secrets but no address, so Central skips it and no bucket can be
created against it.

`active` is not "the nodes joined". It is "this cluster can hold an object", which needs an
applied layout as well — before that Garage answers but its nodes carry no storage role. So a
cluster reports active twice over its life: never on setup alone, and then once the layout is
applied.

A cluster reporting `active: false` sends no endpoints: it has none to offer while it is
down, and the ones Central holds are the last known good. **Its secrets are kept**, so a
retry reuses them and the nodes still recognise each other.

## Who holds which key

Cargo keeps the powerful token. Central gets a limited one.

Once a cluster is running, Cargo asks Garage for a second admin token scoped to only the
bucket and key operations Central actually performs — `CreateBucket`, `AddBucketAlias`,
`GetBucketInfo`, `DeleteBucket`, `CreateKey`, `AllowBucketKey`, `DeleteKey`, `GetKeyInfo`.
The cluster's real admin token, which can do anything including changing the layout, never
leaves Cargo.

Checked against Garage v2.3.0: asking for a token with a limited `scope` works. Not wired up
yet — it belongs with the configuration step.
