# fake proxy

> **Test scaffolding.** Same standing as `fake_atlas`: written for local testing and CI, not reviewed, not hardened. Use it for nothing else.

Cargo and Central both tell a regional Proxy where a name lives. This remembers what they said and lets you read it back. It answers the same URLs with the same JSON, so **you never have to change either app** — point Cargo Settings' Proxy URL at this and build a cluster.

## Running it

```bash
python3 tools/fake_proxy/fake_proxy.py --port 8200
```

Use `--flaky N` to have the first N writes to each collection answered `503`. `ProxyClient` treats that as retryable and tries three times, so `--flaky 2` proves the retry works and `--flaky 3` proves the failure is reported.

## What it does

| A client asks for | this does |
|---|---|
| `PATCH /v1/sites/<name>` with `{"address": …}` | records the address, answers `200` with it |
| `DELETE /v1/sites/<name>` | forgets it, answers `204` — also when it was never mapped |
| `PATCH /v1/domains/<domain>` | the same, for custom domains |
| `DELETE /v1/domains/<domain>` | the same |
| `GET /v1/sites`, `GET /v1/domains` | lists what has been mapped |

Any bearer token is accepted. A request with no `Authorization` header is refused with `401`, so a client that forgot it fails here rather than in production.

The two `GET` routes are not part of the real Proxy API. They exist so a test can assert the mapping actually happened.

## What it is for

The domain rules in `ProxyClient._site_name` are unit-tested against strings. Nothing checks that a real build produces domains that satisfy them. Run a build against this and read the mapping back:

```bash
curl -s -H 'Authorization: Bearer x' http://127.0.0.1:8200/v1/sites
{"items": [{"name": "s3-svc", "address": "fdaa:1::10"}, {"name": "s3-admin-svc", "address": "fdaa:1::10"}]}
```

An object storage cluster should map `s3-svc` and `s3-admin-svc`; a datum host should map `telemetry-svc` and `telemetry-read-svc`. A name missing from that list is a domain the build never mapped, and a `400` in the server's log is one it built wrongly.

## What it cannot tell you

- **Nothing is routed.** It records an address; it does not serve the name or prove anything listens there.
- **Any token is accepted.** There is no real check, so it says nothing about whether the token Cargo holds is the one the Proxy wants.
- **Addresses are not validated.** The real Proxy wants a mesh IPv6 address. This takes any non-empty string.
- **Order is not checked.** It records the last address written for a name, so a build that maps a name twice looks the same as one that maps it once.
