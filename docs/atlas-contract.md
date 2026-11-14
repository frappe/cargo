# What Cargo needs from Atlas

Cargo asks Atlas for machines, their firewalls, and DNS records for its regional services. It never asks for buckets or tenants. The code that makes these calls is `cargo/atlas_client.py`, so if this changes, that breaks.

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

One machine per call. Cargo asks for them one at a time and tracks each as its own **Machine**.

| Send | What it is |
|---|---|
| `image_id` | The Atlas id of the image to boot: the base System image, or an image Cargo built |
| `cpu_millicores` | CPU entitlement. 1000 millicores equals one core |
| `memory_mib` | Memory. Cargo works in GB and multiplies by 1024 |
| `disk_mib` | Disk, likewise |
| `ssh_keys` | A list of one: root's public key. Cargo keeps the private half |
| `hostname` | The Machine's own name, e.g. `OSC-0001-storage-0001` |
| `metadata` | The machine's `role` |
| `ipv4_internet_access` | Always `true`. The machine reaches the internet without an address of its own |

Cargo sends these fields only when they are set, because an Atlas without affinity rules rejects unknown fields:

| Send | When |
|---|---|
| `tags` | Storage nodes: `cargo-storage-cluster` and `role` |
| `placement_rules` | Storage nodes: one `has_not` rule on `virtual_machine` with the node tags, so a new node avoids the others of its cluster. `within` comes from `storage_node_spread_within`. No rule when `storage_node_spread` is `Off` |
| `is_termination_protected` | `true` for object storage machines and the datum host |
| `public_ipv4` | `auto` for the object storage gateway and the datum host |
| `is_privileged` | `true` for the object storage gateway and the datum host |

Send back the machine, including its `id`. Don't wait for it to boot; Cargo polls.

Only the two service machines ask for a public IPv4, because Caddy on them serves the internet and obtains certificates from Let's Encrypt. Every other machine has no public address. Everything Cargo itself does to a machine, such as SSH, Garage's admin API and Garage peering, goes over the mesh.

## Checking on a machine — `GET /virtual-machines/{id}`

`sync_all_machines` calls this for every Machine that is not Terminated, and Cargo calls it again whenever it needs the current state.

| Send back | What it is |
|---|---|
| `current_state` | See the table below |
| `network.mesh_ipv6` | The mesh address. This is how Cargo reaches the machine |
| `network.public_ipv4` | The public IPv4, if any. Cargo points the service's DNS records at it |
| `network.public_ipv6` | The public IPv6, if any. Cargo records it and does nothing else with it |
| `compute.cpu_millicores`, `compute.memory_mib` | The current shape |
| `disk.size_mib` | The current disk size |
| `is_termination_protected` | The current protection |

Cargo reads the shape, disk and protection on every sync, because they can change at Atlas without Cargo asking. It reads the addresses only while the machine runs. A service machine must have its public IPv4 before setup installs Caddy, or setup stops with an error.

A machine that is `running` with no mesh address is marked **Broken** rather than waited on. Atlas says it is up, so an address that never came is a fault, not a delay. Cargo derives nothing about the address itself: it records the one Atlas reports, because a wrong address is worse than none.

### States

| `current_state` | Machine status |
|---|---|
| `running` | Running, or Broken without a mesh address |
| `stopped` | Stopped |
| `paused` | Paused |
| `failed` | Broken, with Atlas's `error` |
| Anything else | Unchanged. Cargo waits |

A 404 marks the machine Terminated. Cargo treats only `failed` as terminal. If Atlas can report another state for a machine that will not come back, Cargo needs to know, because today it waits on that state forever.

## Throwing a machine away — `DELETE /virtual-machines/{id}`

Cargo calls this while cleaning up, so it must be safe to call twice. Once cleanup finishes the route answers 404, and that is how Cargo knows termination is done. A refusal leaves the Machine Broken, because the VM still runs.

Image builds call it when a build finishes or fails, so a build never leaves a machine running.

## Termination protection — `PATCH /virtual-machines/{id}/termination-protection`

Send `{"enabled": true}` or `{"enabled": false}`. Atlas refuses to terminate a protected machine. An operator sets it from the **Machine** form. When Cargo releases a protected machine on purpose, it clears the protection first.

## Replacing metadata — `PUT /virtual-machines/{id}/metadata`

Send `{"metadata": {...}}`. Atlas replaces the whole map, so Cargo always sends every key again. A machine reads its metadata from MMDS, so secrets never go through an SSH script.

| Key | Sent for |
|---|---|
| `role` | Every machine at creation, and again with each replace |
| `datum_telemetry_token` | Object storage machines and the Datum host, before the Datum agent is installed |
| `metadata_bucket_access_key`, `metadata_bucket_secret_key` | Object storage machines, when the cluster backs up its metadata |

## Power — `POST /virtual-machines/{id}/actions/{action}`

`action` is `start`, `stop`, `pause`, `resume` or `restart`. Atlas answers before the machine gets there, and the next sync records the new state. An operator sends these from the **Machine** form. An image build stops its build machine before the snapshot, and waits until Atlas reports `stopped`.

## Growing a disk — `PATCH /virtual-machines/{id}/disk`

Send `{"disk_mib": ...}`. Atlas grows the disk while the machine runs. Cargo sends only a larger size. An operator sends it from the **Machine** form, and an object storage cluster sends it for each storage node when its disks grow. The next sync records the new size.

## Resizing — `POST /virtual-machines/{id}/actions/resize`

Send `cpu_millicores`, `memory_mib` and `disk_mib`. Atlas resizes only a stopped machine and only grows the disk. Cargo reads the state first, and refuses when the machine is not `stopped` or the disk would shrink. The next sync records the new shape.

## Public addresses — `PUT` and `DELETE /virtual-machines/{id}/public-ipv4` and `/public-ipv6`

An operator can attach or detach a public address from the **Machine** form. To attach, send `{"public_ip": "auto"}` for an address Atlas picks, or the id of a reserved allocation. Atlas answers before the address is live, and the next sync records it. Atlas refuses a public IPv4 with `ipv4_internet_access_required` when the machine has no IPv4 internet access. A detach releases the address, and a reserved allocation stays with the tenant.

## Firewalling a service machine — `PATCH /virtual-machines/{id}/network`

Before it installs Caddy, setup sends `{"firewall": {...}}` for the object storage gateway and the datum host. The firewall replaces the whole previous one. It admits TCP 80, TCP 443 and ICMP from any address, and all traffic from the machine's own `/64`, which holds the tenant-0 VMs in the region. Atlas answers before Metal applies the rules.

These two machines are created with `is_privileged: true`, so tenant VMs can reach them over the mesh. Atlas accepts it from tenant `0` only.

## Naming a service — `PUT /dns-records/{name}`

Send `{"address": "..."}`. Atlas picks an A or AAAA record from the address and sets a 300 second TTL. Cargo sends it on every setup and when an operator selects **Update DNS Records**, so it must be safe to call twice.

For each service domain, such as `s3-svc.<wildcard-domain>`, Cargo points the name at the machine's public IPv4, and `internal-<name>` at its mesh address. Cargo sends these calls after the Caddyfile renders and the firewall is set, and before it installs Caddy.

## Photographing a machine — `POST /virtual-machines/{id}/actions/snapshot`

Cargo builds images by provisioning a build machine, stopping it, and snapshotting its disk. That snapshot is the image; Atlas boots later machines from it by `image_id`.

| Send | What it is |
|---|---|
| `title` | The image record's name. It is unique, so nothing is overwritten |
| `image_type` | `system` |
| `cache_image` | `true` for a warm image. Atlas pre-downloads the artifacts to every host in the region |
| `memory_snapshot` | `true` for a warm image. Atlas records the build machine's shape as the warm-start template, so a machine of that exact shape resumes from memory instead of booting |
| `tags` | The image's tags, by which Cargo finds it later |

Send back the image, including its `id`. Atlas accepts `cache_image` and `memory_snapshot` from tenant `0` only.

Cargo terminates the build machine once the build finishes, so the image must not depend on it surviving.

## Finding the base image — `GET /images?image_type=system&tag=...&limit=100`

Cargo boots its machines and builds its images on the Ubuntu System image. Atlas names an image by a generated id, so Cargo filters by tags: `purpose:base`, `os:Ubuntu`, `os_version:24.04`, and `minimal:true` or `minimal:false`. Atlas returns enabled images newest first, and Cargo takes the first one whose `status` is `available`.

Cargo stops with a clear error when no such image exists, rather than asking Atlas for a machine that cannot boot.

## Checking on an image — `GET /images/{id}`

After a snapshot, Cargo calls this about once a minute until the image is usable.

| `status` | What Cargo does |
|---|---|
| `available` | The build is done |
| `failed`, `deleting`, `archived` | The build fails with Atlas's `transfer_error` |
| Anything else | Waits |

## Image protection — `PATCH /images/{id}/termination-protection`

Send `{"enabled": false}`. Atlas protects every System image when it makes one and refuses to delete a protected image, so Cargo clears the protection before it deletes an image.

## Retiring an image — `DELETE /images/{id}`

Cargo deletes the Atlas image when a build fails, when an operator retires an image from its form, and when `retire_older_images` retires Pilot images that newer builds replace.

Atlas never refuses an image a machine still uses. It archives the image and reclaims the artifacts when the last machine goes, so Cargo sends the call without checking usage. After the call, Cargo reads the image again. A 404, `deleted`, `deleting` or `archived` counts as gone.

Cargo marks its image Retired only after Atlas lets the image go, so a failure leaves the image as it was for the next run rather than leaking the snapshot.

## One tenant, one region

Every call carries `X-Tenant-ID`, and Cargo sends `0` for its whole life. Atlas scopes each route to tenant `0`, so another tenant's machine is a 404 rather than a refusal.

The region matters in the other direction too. Atlas packs the region ID into the second 16-bit group of every mesh address and checks the `atlas-admin:<region-id>` audience. Cargo Settings carries the same `region_id`.
