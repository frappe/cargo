# Pilot images

## Purpose

Cargo bakes the golden images that Atlas boots for tenants. The app ships no image. Cargo rents a build machine from Atlas, installs Pilot on it, snapshots it, and destroys the machine. Atlas keeps the snapshot and serves it to every host in the region.

## Records

An **Image** is one release of one kind. `kind` is `pilot`. `version` is the Pilot release tag the build installs.

An **Image Variant** is one flavour of that release and the snapshot it produced. Select **Generate Variants** on an Image to create one variant for each Frappe version (`version-15`, `version-16`, `develop`) with a site and without a site.

## Requirements

Set `wildcard_domain` in Cargo Settings before you build. The hostname aliases are built from it, and a build stops with an error when it is empty.

Use Pilot `v0.0.32-pre-alpha` or later. Earlier releases have no Central bootstrap state, and their production setup does not enable nginx at boot.

## Build sequence

Select **Build** on an Image Variant.

```text
build            -> keypair, admin password, Atlas VM (Provisioning)
sync_build_vm    -> VM is running and has a mesh address (Building)
run_build flow
  run_provision_script -> conf/pilot/provision.sh over SSH, then wipe identity (Snapshotting)
  take_snapshot        -> Atlas snapshot, destroy the build machine (Available)
```

`sync_build_machines` runs every minute and moves a variant on when Atlas reports its machine running. The build log streams into the variant while the script runs.

A failed build records the failing step, destroys the build machine, and sets the variant to Failed. Build again to retry. The bench name and the site name survive a rebuild, so the same flavour keeps the same contents.

## What the provision script installs

`cargo/image_builder/conf/pilot/provision.sh` runs as root on a bare Ubuntu 24.04 machine.

1. Adds a temporary swap file, because the build machine has the memory the image boots with and that is not enough to build assets.
2. Removes every regular user the base image shipped and creates the bench user `frappe` at uid and gid 1001.
3. Runs the Pilot installer as root, then as the bench user.
4. Creates the bench with the admin domain, pins the Frappe branch in `bench.toml`, initialises the bench, and creates the site when the flavour includes one.
5. Writes the Central bootstrap state and the hostname aliases.
6. Runs `pilot setup production` and `pilot build --force`.
7. Verifies that nginx is enabled at boot and that both aliases answer.
8. Removes the swap file and the build caches.

Cargo wipes the machine identity after the script ends, so the snapshot carries no machine ID, no host keys, and no authorized key.

## Auto bootstrapping

The image is baked in the Central awaiting-bootstrap state: `central.enabled` is true and `central.bootstrapped` is false.

A host in this state serves the pending screen and polls instance metadata for the attribute `pilot-central`. The attribute must hold `central_endpoint`, `central_auth_token`, `jwks_url`, and `jwks_audience_id`. When Central writes it, Pilot saves the JSON Web Key Set issuer, marks the host bootstrapped, and the admin panel opens. No operator step is needed.

Atlas delivers the attribute through the instance metadata service, so nothing in the image holds a credential.

## Hostname aliases

An alias maps the VM hostname that Atlas assigns onto a local target, so a fresh VM answers on a name it did not know at bake time. Cargo writes two, both built from the wildcard domain in Cargo Settings.

| Type | Pattern | Target |
|---|---|---|
| `admin` | `admin-vm-*.<wildcard-domain>` | `admin.local`, the bench admin domain |
| `site` | `site-*.<wildcard-domain>` | the site the flavour was baked with |

A flavour without a site gets only the admin alias. Neither alias redirects, because the edge proxy terminates TLS and these names never resolve to the machine itself.

The aliases are written before `pilot setup production`, because production setup is what renders them into nginx.

## Snapshot flags

Cargo snapshots with `cache_image` and `memory_snapshot`. Atlas accepts both from tenant 0 only.

`cache_image` tells every host in the region to download the artifacts ahead of the first boot. `memory_snapshot` records the build machine's shape at Atlas as the warm-start template, so a tenant VM of that shape starts from memory instead of a cold boot.

Atlas takes the warm template shape from the machine being snapshotted. `BUILD_VCPUS`, `BUILD_MEMORY_MIB`, and `BUILD_DISK_MIB` in `cargo/image_builder/builder.py` are therefore the shape a baked image boots at, not only the shape it bakes on. Change them together with the tenant VM size.
