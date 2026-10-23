# Pilot images

## Purpose

Cargo bakes the golden images that Atlas boots for tenants. The app ships no image. Cargo rents a build machine from Atlas, installs Pilot on it, snapshots it, and destroys the machine. Atlas keeps the snapshot and serves it to every host in the region.

## Records

A **Pilot Image** is one build machine and the one Atlas image it was snapshotted into. Each image type boots on the one before it:

| Type | Boots on | Adds |
|---|---|---|
| Base | Ubuntu 24.04 | The bench user, Pilot, and the bench `default-bench` initialised at `frappe_commit`. No site. |
| Site | The Base image of its build | A bare site `site.local` with no apps. |
| Apps | The newest Site image of its branch when it was created | One signup app and the apps it requires, installed on the site. |

| Field | Meaning |
|---|---|
| `pilot_version` | The Pilot release tag the build installs. |
| `frappe_branch` | One of the branches in **Frappe Branches** in Cargo Settings, such as `version-16`. |
| `frappe_commit` | The frappe/frappe commit the bench is at. A Base image created without one takes the head commit of its branch. Site and Apps images must name one. |
| `signup_app`, `app_commit` | Apps images only: the signup app and the commit of the stable release it installs. |
| `apps` | Apps images only: the app releases installed, in install order, with what the signup app requires first. |
| `snapshot_id` | The Atlas image. Empty once Atlas has deleted it. |

These six fields together are the identity, in a unique database constraint. The site Administrator password is made inside the machine and is never sent back, so Cargo stores no password. Hostname aliases reach the bench and the site, so neither name has to be unique.

## Requirements

Set `wildcard_domain` in Cargo Settings before you build. The hostname aliases are built from it, and a build stops with an error when it is empty.

List the apps in **Signup Apps** in Cargo Settings, such as `erpnext` or `helpdesk`. Each gets its own Apps image. Cargo reads the apps each one requires from the marketplace registry and installs them first, so `hrms` brings `erpnext`. Pilot keeps that registry in `system/registry-cache` and refreshes it on its own.

Use Pilot `v0.0.32-pre-alpha` or later. Earlier releases have no Central bootstrap state, and their production setup does not enable nginx at boot.

## Build sequence

Insert a Pilot Image, or let release tracking insert one.

```text
insert               -> Atlas VM on the boot image (Provisioning)
sync_machines        -> VM is running and has a mesh address (Building)
_create_image flow
  pin_releases         -> record the Frappe version and the app releases
  run_provision_script -> conf/pilot/provision.sh over SSH
  take_snapshot        -> Atlas snapshot (Snapshotting)
  wait_for_snapshot    -> Atlas image available, destroy the build machine (Completed)
```

`sync_all_machines` runs every minute and moves an image on when Atlas reports its machine running.

Running is not the same as booted. Before it sends the script, the build waits up to 60 seconds for the machine to answer a ping, then up to 180 seconds for sshd to answer a trivial command. A machine that does neither fails the build with which step it failed at, instead of a connection timeout.

The build log streams into the record while the script runs.

A failed build records the failing step, destroys the build machine, and sets the image to Failed. Build again to retry.

Cargo holds the private key only while the machine is being baked, and drops it when the script ends.

## What the provision script installs

`cargo/image_builder/conf/pilot/provision.sh` runs as root. Steps 2 to 4 run for one image type only.

1. Adds a temporary 1 GB swap file. The build machine has the shape the image boots with, 1 vCPU and 1 GB by default, and installs and migrations run next to MariaDB.
2. Base: removes every regular user the Ubuntu image shipped, creates the bench user `frappe` at uid and gid 1000, and installs Pilot. It creates the bench with the admin domain, sets `FRAPPE_BRANCH` in `bench.toml`, and runs `pilot init --commit FRAPPE_COMMIT`, which checks out that commit on the branch. The bench follows the branch on update, and Pilot downloads the commit's prebuilt assets from the branch's release. The build stops if Frappe is at another commit. It disables what a Firecracker guest does not use (fwupd, multipathd, ModemManager, udisks2 and the tty1 console), and installs `pilot-vm-memory.service`. At every boot, before swap starts, that unit turns transparent hugepages off, turns zswap on with zstd and a 20% pool, and enables a 1 GB swap file at `/var/lib/pilot/swap` (`VM_SWAP_SIZE`). The file is created at first boot, so the image does not carry it. MariaDB's unit keeps it out of swap.
3. Site: creates the site.
4. Apps: fetches each app in `REQUIRED_APPS` on its release branch at its release commit, with the prebuilt assets of that commit, then installs them on the site in that order.
5. Runs `pilot setup production`.
6. Verifies the image: nginx is enabled at boot, and on Site and Apps images `site.local` answers `/api/method/ping`. The probe calls the loopback worker, because nginx serves the Central pending screen once the Base image has run `setup central`. It waits for the workers rather than reading one cold start as a broken image.
7. Runs `pilot setup central`, which enables Central management and writes the hostname aliases. Site runs it again to write the site alias, which Base had no site for.
8. Installs an enabled, boot-time prewarm service. Atlas starts an isolated guest for five minutes before it captures a memory snapshot; the service reaches the Frappe loopback worker directly and warms `/api/method/ping`, the site home page, `/login`, and an authenticated `/desk` response. It creates a random, transient Administrator password in the temporary warm guest, logs out immediately after the Desk request, and saves neither the password nor its session. The captured disk removes a one-time marker, so a restored or later boot cannot reset the Administrator password. A Base image has no site, so it makes no password and warms the admin API only. A failed prewarm is logged and never prevents a normal boot.
9. Removes the build caches.

Atlas serves the authorized key from instance metadata on every authentication attempt, so no key is written to the disk and the snapshot carries none.

Every build installs a udev rule that grows the root file system while the guest runs. cloud-init grows it only at boot, but a warm start and a live disk resize grow the disk of a running guest. When the kernel reports a new size for `/dev/vda`, the rule starts `pilot-grow-file-system@vda.service`, which runs `resize2fs`. An image built on an earlier Base image gets the rule too.

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

A build is one Pilot version on one Frappe commit of one branch. These jobs then run:

| Job | Runs | Does |
|---|---|---|
| `start_base_images` | Every 2 hours | Reads the newest published Pilot release and the head commit of each branch, and starts a Base image for that pair if it has none. A new Pilot release and a new Frappe commit both lead to the same check, so each build starts once. |
| `start_site_images` | Every minute | Starts a Site image on the newest Completed Base image of each branch, if it has none. |
| `start_apps_images` | Every minute | For each signup app, reads its newest stable release that runs on the newest Completed Site image of each branch. If no Apps image has that release and every app it installs, with what it requires, has [prebuilt assets](https://github.com/frappe/pilot/blob/develop/docs/prebuilt-assets.md) for its commit, it starts one on that Site image. A release starts no image until all of these assets are published, or while a requirement has no stable release. Turn off **Require Prebuilt Assets** in Cargo Settings to start it without the assets. A new Site image alone rebuilds no app: the app's next release builds on it. |
| `retry_failed_images` | Every minute | Restarts the failed Base and Site images of the newest build on each branch, and the newest Apps image of each signup app, up to **Max Automatic Retries** times each. |
| `retire_older_images` | Every minute | Keeps the Base and Site images of the 3 newest completed builds on each branch, and the 3 newest completed Apps images of each signup app. It retires the older ones. An older Site image stays until every Apps image that boots on it has built, so a failed Apps image can still be retried. |

Every Pilot release is a prerelease today, so `start_base_images` reads the release list and not `releases/latest`. It asks GitHub without a token, which permits 60 calls an hour. `start_apps_images` reads releases from the local registry. It asks GitHub only for the asset checksum file of each app that a release with no Apps image installs. This is a release download and not an API call, and Cargo keeps each answer for 10 minutes.

A build is newest by the time Cargo first created an image for it. Each snapshot is a full disk, so an Apps image keeps working after the Site image it booted on is retired.

Retiring deletes the Atlas image and then marks the record Retired. The record changes last, so a failed Atlas call cannot leave an image behind. An image that is building is never retired. Atlas does not refuse an image that a machine still uses: it archives the image and reclaims it when the last machine goes.

While tracking is off, no image is started.

## Snapshot flags

Cargo snapshots with `cache_image` and `memory_snapshot`. Atlas accepts both from tenant 0 only.

`cache_image` tells every host in the region to download the artifacts ahead of the first boot. `memory_snapshot` records the build machine's shape at Atlas as the warm-start template, so a tenant VM of that shape starts from memory instead of a cold boot.

Before it captures that template, Atlas boots a temporary guest for about five minutes. The image's `pilot-prewarm.service` uses that period to load Frappe in the web worker. It calls the loopback upstream rather than nginx because a newly baked image is awaiting Central bootstrap and nginx serves the pending screen in that state.

Atlas takes the warm template shape from the machine being snapshotted, and restores a warm image only when the vCPU count and memory match and the disk is at least as large. `BUILD_CPU_MILLICORES`, `BUILD_MEMORY_MIB`, and `BUILD_DISK_MIB` in `cargo/image_builder/doctype/pilot_image/builder.py` are therefore the shape a baked image boots at, not only the shape it bakes on. Central must ask for the same vCPU count and memory, and a disk at least as large.
