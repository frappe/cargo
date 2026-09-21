# Fake Atlas Pilot Image and Metadata Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Cargo-built Pilot/Frappe `version-16` Docker snapshots discoverable and bootable through Fake Atlas, with per-VM Atlas-compatible metadata at `169.254.169.254`.

**Architecture:** Fake Atlas stores image catalog data in Docker labels and derives runtime fields from `docker image inspect`. Its systemd base image installs a small metadata server; each VM mounts an owner-only JSON file containing only that VM's custom attributes.

**Tech Stack:** Python 3 standard library, `unittest`, Docker CLI, systemd, Docker image labels.

**Spec:** `tools/fake_atlas/PILOT_IMAGE_DESIGN.md`

## Global Constraints

- Every implementation, test, and documentation change stays under `tools/fake_atlas/`.
- Cargo, Central, Pilot, and Cargo's bucket-creation API remain unchanged.
- Pilot must use its production metadata URL and protocol.
- Tests are written and observed failing before their implementation.
- Fake Atlas remains local and CI test scaffolding, not a production service.

---

### Task 1: Atlas-compatible metadata server

**Files:**
- Create: `tools/fake_atlas/metadata_server.py`
- Create: `tools/fake_atlas/test_metadata_server.py`

**Interfaces:**
- Produces: `MetadataState(attributes: dict[str, str], clock: Callable[[], float] = time.monotonic)`.
- Produces: `make_handler(state: MetadataState) -> type[BaseHTTPRequestHandler]`.
- Produces: `serve(metadata_path: str, host: str = "169.254.169.254", port: int = 80) -> None`.

- [ ] **Step 1: Write failing protocol tests**

Create tests that run a `ThreadingHTTPServer` on `127.0.0.1:0` with `make_handler`. Assert that a token request without a positive integer TTL returns 400, a valid request returns a non-empty token, an attribute read without that token returns 401, the valid token returns the exact plain-text value, an unknown attribute returns 404, and an expired token returns 401 using an injected clock.

```python
def test_token_is_required_to_read_an_attribute(self):
	status, _ = self.request("GET", "/latest/meta-data/attributes/pilot-central")
	self.assertEqual(status, 401)

def test_issued_token_reads_the_exact_attribute(self):
	status, token = self.request(
		"PUT",
		"/latest/api/token",
		{"X-metadata-token-ttl-seconds": "60"},
	)
	self.assertEqual(status, 200)
	status, body = self.request(
		"GET",
		"/latest/meta-data/attributes/pilot-central",
		{"X-metadata-token": token},
	)
	self.assertEqual((status, body), (200, '{"central_endpoint":"https://central.test"}'))
```

- [ ] **Step 2: Run the metadata tests and verify RED**

Run: `python3 -m unittest tools.fake_atlas.test_metadata_server -v`

Expected: import failure because `tools.fake_atlas.metadata_server` does not exist.

- [ ] **Step 3: Implement the minimal metadata server**

Implement an in-memory token map with expiry timestamps. Accept only `PUT /latest/api/token` and `GET /latest/meta-data/attributes/<URL-decoded-key>`. Bound token TTL to `1..21600`, use `secrets.token_urlsafe(32)`, suppress request logs, and return text bodies with explicit content length.

`serve` reads the mounted JSON file, rejects a non-object or non-string values, constructs the handler, and calls `ThreadingHTTPServer((host, port), handler).serve_forever()`.

- [ ] **Step 4: Run metadata tests and verify GREEN**

Run: `python3 -m unittest tools.fake_atlas.test_metadata_server -v`

Expected: all metadata protocol tests pass.

- [ ] **Step 5: Commit the metadata server**

```bash
git add tools/fake_atlas/metadata_server.py tools/fake_atlas/test_metadata_server.py
git commit -m "feat(fake-atlas): emulate instance metadata"
```

### Task 2: Persistent Docker-backed image catalog

**Files:**
- Modify: `tools/fake_atlas/fake_atlas.py`
- Modify: `tools/fake_atlas/test_fake_atlas.py`

**Interfaces:**
- Produces: `inspect_image(image_id: str) -> dict | None`.
- Produces: `image_response(image_id: str) -> dict` with Atlas fields `id`, `title`, `image_type`, `architecture`, `status`, `enabled`, `rootfs_size_mib`, `created_at`, and `tags`.
- Produces: `list_images(query: str) -> dict` with tag filtering and pagination.
- Stores: Docker labels `io.frappe.fake-atlas.image-type`, `io.frappe.fake-atlas.title`, and `io.frappe.fake-atlas.tags`.

- [ ] **Step 1: Write failing image catalog tests**

Mock the Docker inspection seam, not `image_response`. Assert the built-in Ubuntu image advertises `purpose=base`, snapshot labels round-trip into a complete System image, `purpose=pilot` filtering excludes the base image, offset/limit are reflected in the response, and an unknown image raises `KeyError`.

```python
@patch("tools.fake_atlas.fake_atlas.docker_images")
def test_pilot_tag_returns_only_the_snapshot(self, docker_images):
	docker_images.return_value = [SNAPSHOT_INSPECT]
	page = list_images("image_type=system&tag=purpose:pilot&offset=0&limit=100")
	self.assertEqual([item["id"] for item in page["items"]], ["cargo-snapshot/pilot"])
```

Add an HTTP assertion that the existing tagged base-image query still returns `ubuntu-24.04`.

- [ ] **Step 2: Run focused catalog tests and verify RED**

Run: `python3 -m unittest tools.fake_atlas.test_fake_atlas.TestImageCatalog -v`

Expected: import or attribute failures for the missing catalog functions.

- [ ] **Step 3: Implement image inspection and filtering**

Add label constants and Docker inspection helpers. Parse Docker's RFC3339 creation timestamp to epoch seconds, round size up to MiB, normalize Docker architecture names to Atlas strings, and decode the JSON tag label only when it is a `dict[str, str]`.

Represent `ubuntu-24.04` dynamically with complete Atlas fields. Read query parameters with `parse_qs`, require integer `offset >= 0` and `1 <= limit <= 100`, filter by `image_type` and every comma-separated `key:value` tag, sort newest first, and return `has_more` based on the filtered collection.

- [ ] **Step 4: Persist snapshot metadata in Docker labels**

Validate `image_type` as `machine` or `system` and validate `tags` as non-empty string keys and string values without control characters. Pass labels through `docker commit --change` and return `image_response(tag)` so Cargo sees the complete created image.

- [ ] **Step 5: Run catalog tests and verify GREEN**

Run: `python3 -m unittest tools.fake_atlas.test_fake_atlas.TestImageCatalog -v`

Expected: all image catalog tests pass.

- [ ] **Step 6: Commit the image catalog**

```bash
git add tools/fake_atlas/fake_atlas.py tools/fake_atlas/test_fake_atlas.py
git commit -m "feat(fake-atlas): expose docker snapshots as images"
```

### Task 3: Boot requested snapshots with isolated metadata

**Files:**
- Modify: `tools/fake_atlas/fake_atlas.py`
- Modify: `tools/fake_atlas/test_fake_atlas.py`

**Interfaces:**
- Produces: `write_vm_metadata(vm_id: str, metadata: dict[str, str]) -> str`.
- Produces: `remove_vm_metadata(vm: dict) -> None`.
- Changes: `boot(..., metadata_path: str | None)` mounts the file at `/run/fake-atlas/metadata.json:ro`.
- Changes: `create_virtual_machine(payload)` resolves `payload["image_id"]` and passes the request metadata to the VM.

- [ ] **Step 1: Write failing VM lifecycle tests**

Assert that VM creation rejects an unknown `image_id`, selects an existing `cargo-snapshot/pilot` image verbatim, writes distinct metadata files for two VMs with mode `0600`, adds a read-only bind mount to `docker run`, and removes the matching metadata file during termination.

```python
def test_each_vm_gets_its_own_metadata_file(self):
	first = write_vm_metadata("vm-one", {"pilot-central": "one"})
	second = write_vm_metadata("vm-two", {"pilot-central": "two"})
	self.assertNotEqual(first, second)
	self.assertEqual(json.loads(Path(first).read_text())["pilot-central"], "one")
	self.assertEqual(stat.S_IMODE(Path(first).stat().st_mode), 0o600)
```

- [ ] **Step 2: Run focused VM tests and verify RED**

Run: `python3 -m unittest tools.fake_atlas.test_fake_atlas.TestVirtualMachineImages tools.fake_atlas.test_fake_atlas.TestMetadataFiles -v`

Expected: failures for missing validation, snapshot selection, and metadata lifecycle functions.

- [ ] **Step 3: Install the metadata service in versioned systemd images**

Change the local systemd tag to include a metadata image version. Build with `tools/fake_atlas/` as context, copy `metadata_server.py`, install `python3-minimal` and `iproute2`, and enable this unit:

```ini
[Unit]
Description=Fake Atlas instance metadata
After=network.target
Before=multi-user.target

[Service]
ExecStartPre=/bin/mkdir -p /run/fake-atlas
ExecStartPre=/usr/sbin/ip address replace 169.254.169.254/32 dev lo
ExecStart=/usr/bin/python3 /usr/local/lib/fake-atlas/metadata_server.py /run/fake-atlas/metadata.json
Restart=on-failure
RestartSec=1

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Implement VM image and metadata lifecycle**

Resolve built-in IDs through `IMAGES`; otherwise require `docker image inspect` success. Serialize only a `dict[str, str]` metadata map with `0600` permissions under one process temporary directory. Store the path on the VM record, mount it read-only, and delete it on termination or boot failure. Register normal process-exit cleanup for the temporary directory.

Only systemd mode enables the metadata service. Preserve the existing SSH and admin-port behavior.

- [ ] **Step 5: Run VM lifecycle tests and verify GREEN**

Run: `python3 -m unittest tools.fake_atlas.test_fake_atlas.TestVirtualMachineImages tools.fake_atlas.test_fake_atlas.TestMetadataFiles -v`

Expected: all VM image-selection and metadata lifecycle tests pass.

- [ ] **Step 6: Commit VM metadata delivery**

```bash
git add tools/fake_atlas/fake_atlas.py tools/fake_atlas/test_fake_atlas.py
git commit -m "feat(fake-atlas): inject per-vm metadata"
```

### Task 4: Documentation and full proof

**Files:**
- Modify: `tools/fake_atlas/README.md`

**Interfaces:**
- Documents: Cargo image build, Central image discovery, metadata behavior, credential cleanup, and end-to-end commands.

- [ ] **Step 1: Document the end-to-end flow**

Update the README to explain that `--systemd` is required for Pilot, Cargo snapshots persist as Docker images, Central can discover `purpose=pilot`, and tenant metadata is locally sensitive. Include inspection commands for the metadata unit and Pilot bootstrap log, plus cleanup commands that remove Fake Atlas containers, network, snapshots, and temporary metadata.

- [ ] **Step 2: Run all Fake Atlas tests**

Run: `python3 -m unittest discover -s tools/fake_atlas -p 'test_*.py' -v`

Expected: all tests pass with no errors.

- [ ] **Step 3: Run repository formatting and static checks on changed Python files**

Run: `.venv/bin/ruff check tools/fake_atlas/fake_atlas.py tools/fake_atlas/metadata_server.py tools/fake_atlas/test_fake_atlas.py tools/fake_atlas/test_metadata_server.py`

Run: `.venv/bin/ruff format --check tools/fake_atlas/fake_atlas.py tools/fake_atlas/metadata_server.py tools/fake_atlas/test_fake_atlas.py tools/fake_atlas/test_metadata_server.py`

Expected: both commands exit successfully.

- [ ] **Step 4: Review the complete diff**

Invoke the required code-review skill because this changes provisioning behavior across more than three files. Resolve findings only within `tools/fake_atlas/`, then rerun the focused suite and static checks.

- [ ] **Step 5: Commit documentation and reviewed integration**

```bash
git add tools/fake_atlas/README.md tools/fake_atlas/test_fake_atlas.py tools/fake_atlas/test_metadata_server.py tools/fake_atlas/fake_atlas.py tools/fake_atlas/metadata_server.py
git commit -m "docs(fake-atlas): explain pilot provisioning flow"
```
