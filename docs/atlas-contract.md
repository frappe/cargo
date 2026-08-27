# Atlas contract for Cargo

What Cargo needs from Atlas to provision service machines. **Draft — not agreed with the
Atlas team.** `cargo/object_storage/atlas_client.py` is written against this, so a change
on either side breaks provisioning until both move.

Cargo asks Atlas only for machines. Never for buckets, tenants, or services.

## Transport

- `POST {atlas_url}/api/method/atlas.atlas.api.service.<fn>`, JSON in and out
- Responses are unwrapped from Frappe's `{"message": ...}` envelope
- Errors may arrive as **HTTP 200 with an `exc` / `_server_messages` payload**, so a status
  check alone cannot tell success from failure

### Authentication

`Authorization: Bearer <token>` — minted and RS256-signed by Central, verified by Atlas
against Central's JWKS at `{central_url}/api/method/central.api.jwks.get_jwks`. No shared
secret, and revocation is Central rotating its signing key.

> **Open:** Atlas authenticates service callers with `token <key>:<secret>` today. Bearer
> is the agreed direction, not yet implemented on the Atlas side.

## `create_bare_vms`

Creates a whole placement group in one call.

| Field | Type | Notes |
|---|---|---|
| `title` | string | Human label for the group |
| `base_image` | string | e.g. `ubuntu-22.04` |
| `placement_group` | object | See below |
| `ssh_public_key` | string | Stamped onto every machine; the private half never leaves Cargo |

```json
{
  "strategy": "partition",
  "topology_key": "rack",
  "partition_count": 2,
  "instance_count": 4,
  "spec": { "cpu": 2, "ram_gb": 4, "disk_gb": 100 }
}
```

Returns `{"vm_ids": ["...", "..."]}` — one id per instance, as soon as the request is
accepted. The machines are still booting and have no address yet.

> **Open:** this endpoint does not exist. Atlas creates one VM at a time
> (`create_bare_vm`) with an optional `server` pin and no notion of a placement group.
>
> Cargo needs the group honoured as a real constraint, and the request **failed** when it
> cannot be satisfied rather than placed anyway. Garage's replication assumes independent
> failure domains: three replicas in one rack survive a disk, not a rack. Approximating
> this by pinning nodes to different servers spreads across hosts rather than racks, and
> degrades silently when there are fewer servers than nodes.

## `get_virtual_machine`

`{"name": "<vm-id>"}` → the VM. Polled on a schedule until the machine is usable.

| Field | Type | Notes |
|---|---|---|
| `name` | string | |
| `status` | string | `Running` means booted; `Failed`/`Error`/`Terminated`/`Archived`/`Broken` are terminal |
| `ipv4_address` | string | **Public and reachable** |
| `server` | string | The host it landed on |

Cargo treats `Running` **without** an `ipv4_address` as still booting, not as ready.

> **Assumption Cargo is built on:** every service VM gets a public, reachable
> `ipv4_address`. These are infrastructure machines, not tenant benches — no ProxyJump
> through a hypervisor, no private network. If Atlas cannot guarantee this, Cargo's SSH
> path changes.

## `terminate_vm`

`{"vm": "<vm-id>"}`. Must be idempotent — used in cleanup where the VM may already be gone.
