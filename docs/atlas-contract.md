# What Cargo needs from Atlas

**Draft. Not agreed with the Atlas team yet.**

Cargo asks Atlas for machines. That's all — never buckets, tenants, or services. The code
that makes these calls is `cargo/atlas_client.py`, so if this changes, that
breaks.

## How the calls work

Every call is a POST to:

```
{atlas_url}/api/method/atlas.atlas.api.service.<name>
```

JSON in, JSON out. The answer comes wrapped as `{"message": ...}` and Cargo unwraps it.

**Watch out:** Frappe can return HTTP 200 with an error inside the body (`exc` or
`_server_messages`). Checking the status code alone isn't enough, so Cargo checks both.

### Logging in

```
X-Cargo-Token: <atlas_access_token>
```

Central signs the token with its private key. Atlas checks it against Central's public keys
at `{central_url}/api/method/central.api.jwks.get_jwks`. Nothing is shared between Cargo and
Atlas, and Central can revoke everything by rotating its key.

The header is `X-Cargo-Token`, not `Authorization`: Frappe rejects an unrecognised
`Authorization` header with a 401 before the endpoint is reached. The token's scope is
`cargo:atlas`, and it is a different token from the one Cargo presents to Central.

> **Not built yet.** Atlas currently uses `token <key>:<secret>` for service callers. We
> agreed on bearer tokens, but nothing has changed on the Atlas side.

## Making machines — `create_bare_vms`

Cargo asks for a whole cluster in one call, or for a single machine.

| Send | What it is |
|---|---|
| `title` | A label for the group |
| `base_image` | e.g. `ubuntu-22.04`, `ubuntu-24.04` |
| `placement_group` | The machines wanted, below. **Omitted entirely when Cargo does not care where the machine lands** — an image build wants one machine, anywhere |
| `ssh_public_key` | Put on every machine as root's key. Cargo keeps the private half |

With no `placement_group`, send back exactly one id.

```json
{
  "strategy": "partition",
  "topology_key": "rack",
  "partition_count": 2,
  "specs": [
    { "role": "gateway", "count": 1, "cpu": 2, "ram_gb": 4, "disk_gb": 20 },
    { "role": "storage", "count": 3, "cpu": 2, "ram_gb": 4, "disk_gb": 500 }
  ]
}
```

One entry per role, because a gateway just passes traffic through and barely needs a disk,
while a storage node holds all the data.

Send back `{"vm_ids": [...]}` as soon as you accept the request. Don't wait for the machines
to boot — Cargo polls for that.

**Order matters.** Return the ids in the same order as `specs`, one per `count`. Cargo
decides which machine is the gateway purely by position, so a shuffled list labels every
machine wrong.

> **Not built yet.** Atlas makes one VM at a time (`create_bare_vm`) and has no idea what a
> placement group is.
>
> Cargo needs the spread to actually happen, and needs the call to **fail** if it can't
> rather than putting the machines anywhere. Garage keeps copies of data on separate
> machines to survive a failure — but if all three copies land in the same rack, they die
> together. Cargo can fake this today by pinning machines to different servers, which
> spreads them across hosts rather than racks, and quietly stops working when there are
> fewer servers than machines.

## Checking on a machine — `get_virtual_machine`

Send `{"name": "<vm-id>"}`. Cargo calls this every 2 minutes until the machine is usable.

| Send back | What it is |
|---|---|
| `name` | The id |
| `status` | `Running` when booted. `Failed`, `Error`, `Terminated`, `Archived`, `Broken` mean it's never coming up |
| `error` | Why, when the status is one of the dead ones. Optional, and the only way Cargo can tell an operator what went wrong — a bare `Broken` leaves nothing to act on |
| `ipv4_address` | A public address that Cargo can SSH to |
| `server` | Which host it ended up on |

A machine that says `Running` but has no address is still starting up, and Cargo keeps
waiting.

> **What Cargo assumes.** Every machine gets a public IPv4 address you can reach from the
> internet. These are infrastructure machines, not customer benches — there's no jumping
> through a hypervisor and no private network. If Atlas can't promise this, Cargo's whole
> approach to reaching machines has to change.

## Photographing a machine — `create_snapshot`

Cargo builds golden images by provisioning a throwaway machine and snapshotting its disk.
That snapshot is the image; Atlas boots later machines from it.

| Send | What it is |
|---|---|
| `vm` | The machine to snapshot. It is running and has been provisioned |
| `title` | What to call the snapshot. Unique per image variant, so nothing is overwritten |

Send back `{"snapshot_id": "..."}`.

Cargo terminates the machine straight afterwards, so the snapshot must not depend on it
surviving.

> **Not built yet.** Neither snapshot call exists on the Atlas side. Cargo's shape is a
> guess and can move to match whatever Atlas offers.

## Checking on a snapshot — `get_snapshot`

Send `{"snapshot": "<snapshot-id>"}`. Cargo uses this to know when an image is usable.

| Send back | What it is |
|---|---|
| `snapshot_id` | The id |
| `status` | `Available` once it can be booted from |
| `size` | Bytes, so an operator can see what a variant costs to keep |

## Throwing a machine away — `terminate_vm`

Send `{"vm": "<vm-id>"}`. Cargo calls this while cleaning up, so it must be safe to call on
a machine that's already gone. Image builds call it on every path, including failures, so a
build never leaves a machine running.
