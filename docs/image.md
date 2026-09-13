# Pilot images

## Purpose

Cargo bakes the golden images that Atlas boots for tenants. The app ships no image. Cargo rents a build machine from Atlas, installs Pilot on it, snapshots it, and destroys the machine. Atlas keeps the snapshot and serves it to every host in the region.

## Records

An **Image** is one Pilot release baked against one Frappe version, and the snapshot it produced. `pilot_version` is the release tag the build installs. `frappe_version` is `version-16` or `develop`.

Every image carries the same bench `default-bench` and the same site `site.local`. Both are reached through a hostname alias, so neither has to be unique. The site Administrator password is made inside the machine and is never sent back, so Cargo stores no password.

One pair is one image, so a Pilot release has two images.

## Requirements

Set `wildcard_domain` in Cargo Settings before you build. The hostname aliases are built from it, and a build stops with an error when it is empty.

Use Pilot `v0.0.32-pre-alpha` or later. Earlier releases have no Central bootstrap state, and their production setup does not enable nginx at boot.

## Build sequence

Select **Build** on an Image.

```text
build            -> keypair, Atlas VM (Provisioning)
sync_build_vm    -> VM is running and has a mesh address (Building)
run_build flow
  run_provision_script -> conf/pilot/provision.sh over SSH (Snapshotting)
  take_snapshot        -> Atlas snapshot, destroy the build machine (Available)
```

`sync_build_machines` runs every minute and moves an image on when Atlas reports its machine running.

Running is not the same as booted. Before it sends the script, the build waits up to 60 seconds for the machine to answer a ping, then up to 180 seconds for sshd to answer a trivial command. A machine that does neither fails the build with which step it failed at, instead of a connection timeout.

The build log streams into the record while the script runs.

A failed build records the failing step, destroys the build machine, and sets the image to Failed. Build again to retry.

Cargo holds the private key only while the machine is being baked, and drops it when the script ends.

## What the provision script installs

`cargo/image_builder/conf/pilot/provision.sh` runs as root on a bare Ubuntu 24.04 machine.

1. Adds a temporary swap file, because the build machine has the memory the image boots with and that is not enough to build assets.
2. Removes every regular user the base image shipped and creates the bench user `frappe` at uid and gid 1000.
3. Runs the Pilot installer as root, then as the bench user.
4. Makes the site Administrator password, creates the bench with the admin domain, pins the Frappe branch in `bench.toml`, initialises the bench, and creates the site.
5. Runs `pilot setup production`.
6. Verifies the image: nginx is enabled at boot, and `site.local` answers `/api/method/ping`. The probe uses `curl --resolve` against `127.0.0.1`, so it tests the machine it runs on and reaches no network, and it waits for the workers rather than reading one cold start as a broken image.
7. Runs `pilot setup central`, which enables Central management and writes the hostname aliases. It comes last because it puts the host in the awaiting-bootstrap state, where the pending screen replaces the site the step above probes.
8. Installs an enabled, boot-time prewarm service. Atlas starts an isolated guest for five minutes before it captures a memory snapshot; the service reaches the Frappe loopback worker directly and warms `/api/method/ping`, the site home page, `/login`, and an authenticated `/desk` response. It creates a random, transient Administrator password in the temporary warm guest, logs out immediately after the Desk request, and saves neither the password nor its session. The captured disk removes a one-time marker, so a restored or later boot cannot reset the Administrator password. A failed prewarm is logged and never prevents a normal boot.
9. Removes the swap file and the build caches.

Atlas serves the authorized key from instance metadata on every authentication attempt, so no key is written to the disk and the snapshot carries none.

## Auto bootstrapping

The image is baked in the Central awaiting-bootstrap state: `central.enabled` is true and `central.bootstrapped` is false.

A host in this state serves the pending screen and polls instance metadata for the attribute `pilot-central`. The attribute must hold `central_endpoint`, `central_auth_token`, `jwks_url`, and `jwks_audience_id`. When Central writes it, Pilot saves the JSON Web Key Set issuer, marks the host bootstrapped, and the admin panel opens. No operator step is needed.

Atlas delivers the attribute through the instance metadata service, so nothing in the image holds a credential.

## Hostname aliases

An alias maps the VM hostname that Atlas assigns onto a local target, so a fresh VM answers on a name it did not know at bake time. `pilot setup central` writes two, both built from the wildcard domain in Cargo Settings.

| Type | Pattern | Target |
|---|---|---|
| `admin` | `admin-vm-*.<wildcard-domain>` | `admin.local`, the bench admin domain |
| `site` | `site-*.<wildcard-domain>` | the site the image was baked with |

Neither alias redirects, because the edge proxy terminates TLS and these names never resolve to the machine itself.

The aliases are written after `pilot setup production`, so `pilot setup central` rewrites nginx and rebuilds the process set itself.

## Release tracking

Cargo can follow Pilot itself. Turn on **Track Pilot Releases** in Cargo Settings. It is off by default, and a person or another system can turn it on.

An hourly job then:

1. Reads the newest published Pilot release. Every Pilot release is a prerelease today, so the job reads the release list and not `releases/latest`.
2. Creates an image for `version-16` and `develop` if they are absent, and builds each one.
3. Retires everything outside the newest 3 Pilot versions, which keeps 6 images.

A version is newest by the time Cargo first created an image for it.

Retiring deletes the Atlas snapshot and then the Cargo record. The record goes last, so a failed Atlas call cannot leave a snapshot behind. An image that is building is never retired. Atlas does not refuse an image that a machine still uses: it archives the image and reclaims it when the last machine goes.

While tracking is off, nothing is created and nothing is retired.

## Snapshot flags

Cargo snapshots with `cache_image` and `memory_snapshot`. Atlas accepts both from tenant 0 only.

`cache_image` tells every host in the region to download the artifacts ahead of the first boot. `memory_snapshot` records the build machine's shape at Atlas as the warm-start template, so a tenant VM of that shape starts from memory instead of a cold boot.

Before it captures that template, Atlas boots a temporary guest for about five minutes. The image's `pilot-prewarm.service` uses that period to load Frappe in the web worker. It calls the loopback upstream rather than nginx because a newly baked image is awaiting Central bootstrap and nginx serves the pending screen in that state.

Atlas takes the warm template shape from the machine being snapshotted, and restores a warm image only when the vCPU count, memory, and disk all match. `BUILD_VCPUS`, `BUILD_MEMORY_MIB`, and `BUILD_DISK_MIB` in `cargo/image_builder/builder.py` are therefore the shape a baked image boots at, not only the shape it bakes on. Central must ask for the same shape.
