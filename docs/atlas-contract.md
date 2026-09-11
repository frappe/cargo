# What Cargo needs from Atlas

Cargo asks Atlas for machines. That's all — never buckets, tenants, or services. The code
that makes these calls is `cargo/atlas_client.py`, so if this changes, that breaks.

## How the calls work

Atlas's tenant API, REST rather than Frappe method calls:

```
{atlas_url}/api/atlas/<resource>
```

JSON in, JSON out. The body **is** the resource — Atlas unwraps its own `ApiResult`, so there
is no `{"message": ...}` envelope to dig through.

Errors come back as `{"error": {"code", "message", "fields"}}`, and Cargo reads the message
and the per-field ones out of it. A 404 is its own thing (`AtlasNotFound`), because a machine
Atlas no longer has is an answer rather than a failure.

### Authentication

```
Authorization: Bearer <atlas_token>
X-Tenant-ID: 0
```

Atlas provisions the bearer token with the `atlas-admin:<region-id>` audience, `cargo` subject, `*` scope, and tenant `0`. Cargo stores it in the encrypted `atlas_token` field. Every tenant API route is scoped to tenant `0` by the header and the signed tenant claim.

## Making a machine — `POST /virtual-machines`

One machine per call. Cargo asks for them one at a time and tracks each as its own
**Machine**.

| Send | What it is |
|---|---|
| `image_id` | What to boot, e.g. `ubuntu-24.04`, or a snapshot Cargo made earlier |
| `vcpus` | Cores |
| `memory_mib` | Memory. Cargo works in GB and multiplies by 1024 |
| `disk_mib` | Disk, likewise |
| `ssh_keys` | A list of one: root's public key. Cargo keeps the private half |
| `hostname` | The Machine's own name, e.g. `OSC-0001-storage-0001` |
| `metadata` | Free-form. Cargo puts the machine's `role` here |
| `egress` | Always `uplink` — see below |

Send back the machine, including its `id`. Don't wait for it to boot; Cargo polls.

**No public address is asked for.** `egress: uplink` gives the machine the internet without an
address of its own. Everything Cargo does to a machine — SSH, Garage's admin API, Garage
peering — goes over the mesh.

## Checking on a machine — `GET /virtual-machines/{id}`

Cargo polls this until the machine is usable, and again whenever it needs the current state.

| Send back | What it is |
|---|---|
| `current_state` | `running` once it is up. `failed` means it is never coming up |
| `wireguard_mesh_ipv6` | The mesh address. This is how Cargo reaches the machine |

A machine that is `running` but has no mesh address is still starting, and Cargo keeps
waiting. Cargo derives nothing about the address itself: it records the one Atlas reports.

> **Not built yet.** `wireguard_mesh_ipv6` is not on `VirtualMachineResponse` upstream. Until
> it is, a machine never gets an address — Cargo will not compute one from the region, tenant
> and VM number itself, because a wrong address is worse than none.

### Dead states

Cargo treats only `failed` as terminal. If Atlas can also report `unknown`, `stopped` or
`paused` for a machine that will not come back, Cargo needs to know — today it would wait on
those forever.

## Throwing a machine away — `DELETE /virtual-machines/{id}`

Cargo calls this while cleaning up, so it must be safe to call twice. Once cleanup finishes
the route answers 404, and that is how Cargo knows termination is done.

Image builds call it on every path, including failures, so a build never leaves a machine
running.

## Photographing a machine — `POST /virtual-machines/{id}/actions/snapshot`

Cargo builds golden images by provisioning a throwaway machine and snapshotting its disk.
That snapshot is the image; Atlas boots later machines from it by `image_id`.

Send `{"title": "..."}` — unique per image variant, so nothing is overwritten. Send back the
snapshot, including its `id`.

Cargo terminates the machine straight afterwards, so the snapshot must not depend on it
surviving.

## Checking on a snapshot — `GET /images/{id}`

The image as Atlas sees it, so Cargo can tell when it is bootable.

> **No caller yet.** `AtlasClient.get_snapshot` exists but nothing uses it: an image variant
> records the snapshot id and moves on. It is here for when a variant has to wait for the
> image to become usable.

## One tenant, one region

Every call carries `X-Tenant-ID`, and Cargo sends `0` for its whole life. Atlas scopes each route to tenant `0`, so another tenant's machine is a 404 rather than a refusal.

The region matters in the other direction too. Atlas packs the region ID into the second 16-bit group of every mesh address and checks the `atlas-admin:<region-id>` audience. Cargo Settings carries the same `region_id`.
