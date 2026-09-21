# Fake Atlas Pilot Image and Metadata Design

## Goal

Close the local Central-to-Pilot provisioning loop without changing Cargo, Central, or Pilot production code. Cargo builds Pilot against Frappe `version-16`, Fake Atlas exposes that snapshot as an Atlas System image, Central boots it, and Pilot reads Central and S3 configuration from an Atlas-compatible metadata endpoint.

## Scope

All implementation and test artifacts live in `tools/fake_atlas/`.

The change does not add another Pilot image builder, alter Cargo's snapshot workflow, modify Central's VM payload, or add a development-only metadata URL to Pilot.

## Image lifecycle

Cargo keeps using the existing Pilot image workflow:

1. Request an Ubuntu build VM from Fake Atlas.
2. Install Pilot and initialize a Frappe `version-16` bench.
3. Ask Fake Atlas to snapshot the build VM with `purpose=pilot` and related tags.
4. Destroy the build VM.

Fake Atlas records the snapshot's Atlas-facing metadata as Docker image labels. The labels contain the image type and Cargo-provided tags; the Docker image itself supplies its creation time, architecture, and size. This makes snapshots discoverable after Fake Atlas restarts.

`GET /api/atlas/images` returns matching System images and implements the tag, offset, and limit fields used by Cargo and Central. `GET /api/atlas/images/<id>` returns the same complete image representation. VM creation validates and boots the requested Docker snapshot instead of silently falling back to Ubuntu.

The built-in Ubuntu image remains available as the `purpose=base`, Ubuntu 24.04 System image used by Cargo's builder.

## Metadata service

The Fake Atlas systemd base image contains a small Python metadata server and a systemd unit. A versioned local Docker tag invalidates previously cached base images that do not contain the service.

For each VM, Fake Atlas writes the request's `metadata` map to a permission-restricted temporary JSON file and mounts it read-only into the container. The container assigns `169.254.169.254/32` to loopback and serves the same paths Pilot uses:

- `PUT /latest/api/token` with `X-metadata-token-ttl-seconds`
- `GET /latest/meta-data/attributes/<key>` with `X-metadata-token`

Issued tokens are process-local and expire according to the requested bounded TTL. Missing or invalid headers are rejected. Unknown attributes return 404. Attribute values are returned as plain text, so Central's existing JSON-encoded `pilot-central` value passes through unchanged.

The metadata server is enabled only in systemd-capable Fake Atlas images. Pilot's bootstrap unit already retries every five seconds, so metadata service and user-service startup do not require additional coupling.

## Credential lifecycle

Metadata files use owner-only permissions and a per-process temporary directory. Terminating a VM removes its file. Fake Atlas removes its temporary directory during normal shutdown. An abnormal process exit may leave an OS temporary directory behind; this is accepted for local-only test scaffolding and is documented.

Metadata is not stored in Docker labels, environment variables, image layers, API responses, or logs. Snapshotting a build VM therefore cannot bake a tenant credential into the resulting image.

## Failure behavior

- An unknown `image_id` rejects VM creation instead of booting an unrelated Ubuntu image.
- Invalid snapshot tags reject snapshot creation.
- Docker inspection or commit failures use Fake Atlas's existing Atlas-shaped error response.
- A container whose metadata service cannot start remains diagnosable through Docker and systemd logs; Pilot continues retrying its bootstrap rather than marking corrupt configuration as complete.
- Termination remains best-effort for container and metadata cleanup.

## Verification

Use syntax and Docker smoke checks for the helper scripts, followed by this end-to-end proof:

1. Start Fake Atlas with `--systemd`.
2. Build a Pilot image for Frappe `version-16` through Cargo.
3. Confirm Central discovers the `purpose=pilot` snapshot.
4. Provision the snapshot through Central.
5. Confirm Pilot marks Central bootstrap complete and stores the injected default S3 configuration.
6. Spawn a site and exercise object-storage-backed behavior.

The final bucket and site checks depend on the local Central, Cargo, Garage, and DNS environment.
