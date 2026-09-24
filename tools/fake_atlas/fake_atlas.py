#!/usr/bin/env python3
"""A stand-in for Atlas that hands out Docker containers instead of VMs.

Speaks the shape Cargo expects -- Atlas's tenant API under /api/atlas, an
`Authorization: Bearer <token>` header and an X-Tenant-ID header -- so nothing in the
Cargo app changes. Point Cargo Settings' Atlas URL at this and build an image for real.

    python3 fake_atlas.py --port 8100

Machines are named cargo-vm1, cargo-vm2 ... and Cargo is handed that name as the machine's
address, in the field real Atlas puts a mesh address in. Point every name at 127.0.0.1 in /etc/hosts once (the banner prints the line) and
HTTP, ssh and Garage's own peering all resolve without the host routing to container IPs,
which macOS will not do.
"""

import argparse
import atexit
import json
import math
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

PREFIX = "/api/atlas"
CONTAINER_PREFIX = "cargo-fake"
IMAGES = {"ubuntu-24.04": "ubuntu:24.04", "ubuntu-22.04": "ubuntu:22.04"}
# What `GET /images` offers as the System image to bake on.
SYSTEM_IMAGE = {
	"id": "ubuntu-24.04",
	"title": "Ubuntu 24.04",
	"image_type": "system",
	"architecture": platform.machine().replace("aarch64", "arm64").replace("x86_64", "amd64"),
	"status": "available",
	"enabled": True,
	"rootfs_size_mib": 64,
	"created_at": int(time.time()),
	"tags": {"purpose": "base", "os": "Ubuntu", "os_version": "24.04"},
}
# Atlas machines take minutes to boot. A few seconds here is enough to prove the variant
# really goes Provisioning -> scheduler sweep -> Building, rather than racing straight through.
BOOT_DELAY = 6
# Colima's own VM holds 0.0.0.0:22, so containers cannot publish on 22 at all. They get a
# high port instead, and Cargo reaches them by a name ssh_config maps to that port -- which
# needs no change to Cargo, since ssh reads its config whatever flags are passed.
FIRST_PORT = 2222
# Cargo asks one machine for the cluster's admin API, always the gateway, and always on this
# port. Only the gateway can publish it, so the port lands on the machine Cargo means.
ADMIN_PORT = 3903
GATEWAY = "gateway"
# Slot names are stable across runs, so /etc/hosts is written once. Container IPs are not.
HOST_PREFIX = "cargo-vm"
SLOTS = 12
# Containers resolve each other by name only on a user-defined network, and Garage peers by
# whatever address Cargo gave it.
NETWORK = "cargo-fake"
SSH_CONFIG = os.path.expanduser("~/.ssh/config.d/fake-atlas")
FAKE_ATLAS_DIRECTORY = Path(__file__).resolve().parent
METADATA_DIRECTORY = Path(tempfile.mkdtemp(prefix="fake-atlas-metadata-"))
METADATA_MOUNT = "/var/lib/fake-atlas/metadata.json"
SYSTEMD_IMAGE_VERSION = 5
IMAGE_TYPE_LABEL = "io.frappe.fake-atlas.image-type"
TITLE_LABEL = "io.frappe.fake-atlas.title"
TAGS_LABEL = "io.frappe.fake-atlas.tags"
# Stock ubuntu images have no /sbin/init, and the usual prebuilt systemd images are amd64
# only. Build one locally instead, so this works on whatever architecture the host is.
SYSTEMD_DOCKERFILE = """
FROM {base}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \
 && apt-get install -y -qq systemd systemd-sysv dbus openssh-server sudo curl ca-certificates python3-minimal iproute2 iptables \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/sshd /var/lib/fake-atlas \
 && systemctl enable ssh
COPY metadata_server.py /usr/local/lib/fake-atlas/metadata_server.py
COPY fake-atlas-metadata.service /etc/systemd/system/fake-atlas-metadata.service
RUN systemctl enable fake-atlas-metadata.service
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
"""

SSH_SETUP = """
set -e
export DEBIAN_FRONTEND=noninteractive
if ! command -v sshd > /dev/null; then
	apt-get update -qq
	apt-get install -y -qq openssh-server sudo curl ca-certificates > /dev/null
fi
mkdir -p /root/.ssh /run/sshd
chmod 700 /root/.ssh
cat > /root/.ssh/authorized_keys <<'KEY'
{public_key}
KEY
chmod 600 /root/.ssh/authorized_keys
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
{start_sshd}
"""

VMS: dict[str, dict] = {}
LOCK = threading.Lock()
atexit.register(shutil.rmtree, METADATA_DIRECTORY, ignore_errors=True)


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
	return subprocess.run(command, capture_output=True, text=True, check=True, **kwargs)


def inspect_image(image_id: str) -> dict | None:
	result = subprocess.run(
		["docker", "image", "inspect", image_id],
		capture_output=True,
		text=True,
	)
	if result.returncode:
		return None
	images = json.loads(result.stdout)
	return images[0] if images else None


def docker_images() -> list[tuple[str, dict]]:
	result = run(
		[
			"docker",
			"image",
			"ls",
			"--filter",
			f"label={IMAGE_TYPE_LABEL}",
			"--format",
			"{{.Repository}}:{{.Tag}}",
		]
	)
	images = []
	for reference in result.stdout.splitlines():
		if reference.startswith("<none>"):
			continue
		image_id = reference.removesuffix(":latest")
		if inspected := inspect_image(reference):
			images.append((image_id, inspected))
	return images


def image_response(image_id: str, inspected: dict | None = None) -> dict:
	if image_id in IMAGES:
		return dict(SYSTEM_IMAGE)

	inspected = inspected or inspect_image(image_id)
	if not inspected:
		raise KeyError(image_id)
	labels = (inspected.get("Config") or {}).get("Labels") or {}
	try:
		tags = json.loads(labels[TAGS_LABEL])
		created_at = int(datetime.fromisoformat(inspected["Created"].replace("Z", "+00:00")).timestamp())
		image_type = labels[IMAGE_TYPE_LABEL]
		title = labels[TITLE_LABEL]
	except (KeyError, TypeError, ValueError) as exception:
		raise KeyError(image_id) from exception
	if image_type not in ("machine", "system") or not _valid_tags(tags):
		raise KeyError(image_id)

	return {
		"id": image_id,
		"title": title,
		"image_type": image_type,
		"architecture": inspected.get("Architecture") or "unknown",
		"status": "available",
		"enabled": True,
		"rootfs_size_mib": max(1, math.ceil(int(inspected.get("Size") or 0) / (1024 * 1024))),
		"created_at": created_at,
		"tags": tags,
	}


def list_images(query: str) -> dict:
	parameters = parse_qs(query)
	try:
		offset = int(parameters.get("offset", ["0"])[0])
		limit = int(parameters.get("limit", ["100"])[0])
	except ValueError as exception:
		raise ValueError("offset and limit must be whole numbers") from exception
	if offset < 0 or not 1 <= limit <= 100:
		raise ValueError("offset must be nonnegative and limit must be between 1 and 100")

	required_tags = {}
	for expression in parameters.get("tag", []):
		for item in expression.split(","):
			key, separator, value = item.partition(":")
			if not separator or not key or not value:
				raise ValueError("tag filters must use key:value")
			required_tags[key] = value

	images = [image_response("ubuntu-24.04")]
	for image_id, inspected in docker_images():
		try:
			images.append(image_response(image_id, inspected))
		except KeyError:
			continue
	image_type = parameters.get("image_type", [None])[0]
	images = [
		image
		for image in images
		if (not image_type or image["image_type"] == image_type)
		and all(image["tags"].get(key) == value for key, value in required_tags.items())
	]
	images.sort(key=lambda image: image["created_at"], reverse=True)

	return {
		"items": images[offset : offset + limit],
		"offset": offset,
		"limit": limit,
		"has_more": offset + limit < len(images),
	}


def _valid_tags(tags: object) -> bool:
	return isinstance(tags, dict) and all(
		isinstance(key, str)
		and bool(key)
		and isinstance(value, str)
		and not any(character in key + value for character in "\r\n")
		for key, value in tags.items()
	)


def wait_for_systemd(name: str, attempts: int = 60) -> None:
	"""systemd needs a moment before it has a bus. `docker run -d` returns long before that,
	so anything using systemctl straight after gets 'Failed to connect to bus'."""
	for _ in range(attempts):
		probe = subprocess.run(
			["docker", "exec", name, "systemctl", "is-system-running"],
			capture_output=True,
			text=True,
		)
		if probe.stdout.strip() in ("running", "degraded"):
			return
		time.sleep(1)

	raise RuntimeError(f"{name}: systemd did not come up")


def systemd_image(base: str) -> str:
	"""Build (once) an image of `base` that can actually boot systemd."""
	tag = f"cargo-fake/systemd-v{SYSTEMD_IMAGE_VERSION}-{base.replace(':', '-')}"
	if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0:
		return tag

	print(f"  building {tag} (first use, takes a minute)", flush=True)
	run(
		["docker", "build", "-t", tag, "-f", "-", str(FAKE_ATLAS_DIRECTORY)],
		input=SYSTEMD_DOCKERFILE.format(base=base),
	)

	return tag


def resolve_image(image_id: str) -> str:
	if not isinstance(image_id, str) or not image_id:
		raise ValueError("image_id is required")
	if image_id in IMAGES:
		return IMAGES[image_id]
	if inspect_image(image_id):
		return image_id
	raise KeyError(image_id)


def write_vm_metadata(vm_id: str, metadata: dict[str, str]) -> str:
	if not _valid_tags(metadata):
		raise ValueError("metadata must be an object of string attributes")
	path = METADATA_DIRECTORY / f"{vm_id}.json"
	file_descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
	with os.fdopen(file_descriptor, "w") as handle:
		json.dump(metadata, handle)
	return str(path)


def remove_vm_metadata(vm: dict) -> None:
	path = vm.get("metadata_path")
	if path:
		try:
			os.unlink(path)
		except FileNotFoundError:
			pass


def metadata_hostnames(metadata: dict[str, str]) -> list[str]:
	try:
		bootstrap = json.loads(metadata.get("pilot-central", "{}"))
	except (TypeError, ValueError):
		return []
	if not isinstance(bootstrap, dict):
		return []
	urls = [bootstrap.get("central_endpoint"), bootstrap.get("jwks_url")]
	storage = bootstrap.get("s3")
	if isinstance(storage, dict):
		urls.append(storage.get("endpoint_url"))

	hostnames = set()
	for url in urls:
		try:
			hostname = urlsplit(url).hostname if isinstance(url, str) else None
		except ValueError:
			continue
		if hostname and hostname not in ("localhost", "127.0.0.1"):
			hostnames.add(hostname)
	return sorted(hostnames)


def published_ssh_ports() -> set[int]:
	shown = subprocess.run(
		["docker", "ps", "--filter", f"name={CONTAINER_PREFIX}-", "--format", "{{.Ports}}"],
		capture_output=True,
		text=True,
	)
	if shown.returncode:
		return set()
	return {int(port) for port in re.findall(r"127\.0\.0\.1:(\d+)->22/tcp", shown.stdout)}


def allocate_vm(vm_id: str) -> dict:
	"""Claim the lowest free slot, which fixes the machine's name and both its ports. Claimed
	under the lock: two concurrent requests would otherwise pick the same one."""
	with LOCK:
		taken = {vm["slot"] for vm in VMS.values()}
		published = published_ssh_ports()
		slot = 1
		while slot in taken or FIRST_PORT + slot - 1 in published:
			slot += 1

		VMS[vm_id] = {
			"state": "pending",
			"slot": slot,
			"address": f"{HOST_PREFIX}{slot}",
			"port": FIRST_PORT + slot - 1,
			"role": None,
			"container": None,
			"image_id": None,
			"metadata_path": None,
		}

		return dict(VMS[vm_id])


def ensure_network() -> None:
	"""Docker resolves container names to addresses only on a user-defined network."""
	if subprocess.run(["docker", "network", "inspect", NETWORK], capture_output=True).returncode:
		run(["docker", "network", "create", NETWORK])


def live_containers() -> set[str] | None:
	"""Container names docker still has, None if it could not be asked. Machines removed by
	hand stay in `VMS`, and docker reuses their IPs, so their blocks shadow the live ones."""
	shown = subprocess.run(
		["docker", "ps", "--filter", f"name={CONTAINER_PREFIX}-", "--format", "{{.Names}}"],
		capture_output=True,
		text=True,
	)

	return set(shown.stdout.split()) if shown.returncode == 0 else None


def write_ssh_config() -> None:
	"""One Host block per live machine. Cargo asks ssh for a name; ssh finds the port here."""
	live = live_containers()
	with LOCK:
		blocks = [
			f"Host {vm['address']}\n"
			f"\tHostName 127.0.0.1\n"
			f"\tPort {vm['port']}\n"
			f"\tUser root\n"
			f"\tStrictHostKeyChecking no\n"
			f"\tUserKnownHostsFile /dev/null\n"
			for vm in VMS.values()
			if vm["address"] and (live is None or vm["container"] in live)
		]
		text = "# Written by fake_atlas.py. Cleared as machines are destroyed.\n\n" + "\n".join(blocks)
		# Written whole, then moved into place: `open(..., "w")` truncates, and an ssh reading
		# it mid-write finds no host at all.
		staged = f"{SSH_CONFIG}.tmp"
		with open(staged, "w") as handle:
			handle.write(text)
		os.chmod(staged, 0o600)
		os.replace(staged, SSH_CONFIG)


def boot(
	vm_id: str,
	machine: dict,
	role: str,
	image: str,
	public_key: str,
	metadata_path: str,
	hostnames: list[str],
	systemd: bool,
	delay: int,
) -> None:
	"""Start the container and get sshd listening, then mark it Running.

	Stays Pending for `delay` seconds first, so Cargo has to come back for it."""
	name = f"{CONTAINER_PREFIX}-{vm_id}"
	host, port = machine["address"], machine["port"]
	command = [
		"docker",
		"create",
		"--name",
		name,
		"--network",
		NETWORK,
		"--network-alias",
		host,
		"--hostname",
		host,
		"-p",
		f"127.0.0.1:{port}:22",
	]
	for hostname in hostnames:
		command += ["--add-host", f"{hostname}:host-gateway"]
	if role == GATEWAY:
		command += ["-p", f"127.0.0.1:{ADMIN_PORT}:{ADMIN_PORT}"]
	if systemd:
		command += ["--privileged", "--cgroupns=host", "-v", "/sys/fs/cgroup:/sys/fs/cgroup:rw"]
	command += [image] + (["/sbin/init"] if systemd else ["sleep", "infinity"])

	try:
		ensure_network()
		run(command)
		if systemd:
			run(["docker", "cp", metadata_path, f"{name}:{METADATA_MOUNT}"])
		run(["docker", "start", name])
		if role == "builder":
			run(
				[
					"docker",
					"exec",
					name,
					"bash",
					"-c",
					"printf '\\nUV_HTTP_TIMEOUT=300\\n' >> /etc/environment; "
					"for command in mkswap swapon swapoff; do "
					"printf '#!/bin/sh\\nexit 0\\n' > /usr/local/sbin/$command; "
					"chmod 755 /usr/local/sbin/$command; done",
				]
			)
		with LOCK:
			VMS[vm_id].update(container=name, role=role)
		write_ssh_config()

		if systemd:
			wait_for_systemd(name)
		start_sshd = "systemctl enable --now ssh" if systemd else "/usr/sbin/sshd"
		run(
			[
				"docker",
				"exec",
				"-i",
				name,
				"bash",
				"-c",
				SSH_SETUP.format(public_key=public_key, start_sshd=start_sshd),
			]
		)
		time.sleep(delay)
	except (subprocess.CalledProcessError, RuntimeError) as exception:
		error = getattr(exception, "stderr", "") or str(exception)
		subprocess.run(["docker", "rm", "-f", name], capture_output=True)
		with LOCK:
			VMS[vm_id].update(state="failed", error=error[:500], container=None)
		remove_vm_metadata(VMS[vm_id])
		print(f"  x {vm_id} broke: {error.strip()[:200]}", flush=True)
		return

	with LOCK:
		VMS[vm_id].update(state="running")
	admin = f", admin {ADMIN_PORT}" if role == GATEWAY else ""
	print(f"  + {vm_id} Running -> {host} (ssh {port}{admin})", flush=True)


class Handler(BaseHTTPRequestHandler):
	systemd = False
	boot_delay = BOOT_DELAY

	def log_message(self, *args) -> None:
		pass

	def reply(self, payload, status: int = 200) -> None:
		"""Atlas answers with the resource itself, and an error as {"error": {...}}."""
		body = b"" if payload is None else json.dumps(payload).encode()
		self.send_response(status)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def fail(self, message: str, status: int, code: str = "invalid_request") -> None:
		self.reply({"error": {"code": code, "message": message, "fields": []}}, status)

	@property
	def route(self) -> list[str]:
		path = urlsplit(self.path).path
		return path[len(PREFIX) :].strip("/").split("/")

	def authenticated(self) -> bool:
		if not self.path.startswith(PREFIX):
			self.fail("unknown endpoint", 404, "not_found")
			return False
		if not (self.headers.get("Authorization") or "").startswith("Bearer "):
			self.fail("Authorization: Bearer <token> required", 401, "authentication_required")
			return False
		if not self.headers.get("X-Tenant-ID"):
			self.fail("The request needs a tenant ID.", 400)
			return False

		return True

	def body(self) -> dict:
		length = int(self.headers.get("Content-Length") or 0)
		return json.loads(self.rfile.read(length) or "{}")

	def do_POST(self) -> None:
		if not self.authenticated():
			return

		route, payload = self.route, self.body()
		try:
			if route == ["virtual-machines"]:
				self.reply(self.create_virtual_machine(payload), 201)
			elif len(route) == 4 and route[0] == "virtual-machines" and route[2:] == ["actions", "snapshot"]:
				self.reply(self.create_snapshot(route[1], payload), 201)
			else:
				self.fail(f"unimplemented: POST {self.path}", 404, "not_found")
		except KeyError:
			self.fail("The resource does not exist.", 404, "not_found")
		except ValueError as exception:
			self.fail(str(exception), 400)
		except Exception as exception:
			self.fail(str(exception), 500, "internal_error")

	def do_GET(self) -> None:
		if not self.authenticated():
			return

		route = self.route
		try:
			if len(route) == 2 and route[0] == "virtual-machines":
				self.reply(self.get_virtual_machine(route[1]))
			elif route == ["images"]:
				self.reply(self.list_images())
			elif len(route) >= 2 and route[0] == "images":
				self.reply(self.get_image(unquote("/".join(route[1:]))))
			else:
				self.fail(f"unimplemented: GET {self.path}", 404, "not_found")
		except KeyError:
			self.fail("The resource does not exist.", 404, "not_found")
		except ValueError as exception:
			self.fail(str(exception), 400)
		except Exception as exception:
			self.fail(str(exception), 500, "internal_error")

	def do_PATCH(self) -> None:
		if not self.authenticated():
			return

		# Atlas refuses to delete a protected image. This fake protects nothing, so it only
		# answers for an image it knows.
		route = self.route
		if len(route) >= 3 and route[0] == "images" and route[-1] == "termination-protection":
			self.body()
			try:
				self.reply(self.get_image(unquote("/".join(route[1:-1]))), 202)
			except KeyError:
				self.fail("The resource does not exist.", 404, "not_found")
		else:
			self.fail(f"unimplemented: PATCH {self.path}", 404, "not_found")

	def do_DELETE(self) -> None:
		if not self.authenticated():
			return

		route = self.route
		if len(route) == 2 and route[0] == "virtual-machines":
			self.reply(self.terminate(route[1]), 202)
		elif len(route) >= 2 and route[0] == "images":
			try:
				self.reply(self.delete_image(unquote("/".join(route[1:]))), 202)
			except KeyError:
				self.fail("The resource does not exist.", 404, "not_found")
		else:
			self.fail(f"unimplemented: DELETE {self.path}", 404, "not_found")

	def create_virtual_machine(self, payload: dict) -> dict:
		"""One container per machine. Returns immediately: Cargo polls for running."""
		metadata = payload.get("metadata") or {}
		if not isinstance(metadata, dict):
			raise ValueError("metadata must be an object")
		role = metadata.get("role")
		image_id = payload.get("image_id")
		image = resolve_image(image_id)
		if self.systemd and image_id in IMAGES:
			image = systemd_image(image)

		vm_id = f"vm-{uuid.uuid4().hex[:8]}"
		machine = allocate_vm(vm_id)
		try:
			metadata_path = write_vm_metadata(vm_id, metadata)
		except Exception:
			with LOCK:
				VMS.pop(vm_id, None)
			raise
		hostnames = metadata_hostnames(metadata)
		with LOCK:
			VMS[vm_id].update(image_id=image_id, metadata_path=metadata_path)
		write_ssh_config()
		print(f"-> create {vm_id} ({role or 'no role'})", flush=True)
		threading.Thread(
			target=boot,
			args=(
				vm_id,
				machine,
				role,
				image,
				(payload.get("ssh_keys") or [""])[0],
				metadata_path,
				hostnames,
				self.systemd,
				self.boot_delay,
			),
			daemon=True,
		).start()

		return self.as_response(vm_id)

	def as_response(self, vm_id: str) -> dict:
		vm = VMS[vm_id]

		return {
			"id": vm_id,
			"tenant_id": int(self.headers.get("X-Tenant-ID")),
			"image_id": vm["image_id"],
			"created_at": int(time.time()),
			"current_state": vm["state"],
			"desired_state": "running",
			"error": None,
			"compute": {"cpu_millicores": 1000, "memory_mib": 1024, "sleep_after_idle_seconds": 0},
			"disk": {"size_mib": 10240, "used_mib": 0, "iops": 0, "throughput_mibps": 0},
			"network": {
				"egress": "uplink",
				# Real Atlas reports a mesh address; here it is the slot name /etc/hosts knows.
				"mesh_ipv6": vm["address"],
			},
		}

	def get_virtual_machine(self, vm_id: str) -> dict:
		return self.as_response(vm_id)

	def create_snapshot(self, vm_id: str, payload: dict) -> dict:
		"""docker commit is the honest analogue of a disk snapshot."""
		vm = VMS[vm_id]
		if vm.get("role") == "builder":
			run(
				[
					"docker",
					"exec",
					vm["container"],
					"rm",
					"-f",
					"/usr/local/sbin/mkswap",
					"/usr/local/sbin/swapon",
					"/usr/local/sbin/swapoff",
				]
			)
			run(
				[
					"docker",
					"exec",
					vm["container"],
					"sed",
					"-i",
					"/^UV_HTTP_TIMEOUT=300$/d",
					"/etc/environment",
				]
			)
		title = str(payload.get("title") or vm_id)
		suffix = re.sub(r"[^a-z0-9_.-]+", "-", title.lower()).strip(".-") or vm_id
		tag = f"cargo-snapshot/{suffix}"
		image_type = payload.get("image_type") or "machine"
		tags = payload.get("tags") or {}
		if image_type not in ("machine", "system"):
			raise ValueError("image_type must be machine or system")
		if not _valid_tags(tags):
			raise ValueError("tags must be an object of string values")
		changes = {
			IMAGE_TYPE_LABEL: image_type,
			TITLE_LABEL: title,
			TAGS_LABEL: json.dumps(tags, separators=(",", ":"), sort_keys=True),
		}
		command = ["docker", "commit"]
		for key, value in changes.items():
			command += ["--change", f"LABEL {key}={json.dumps(value)}"]
		command += [vm["container"], tag]
		run(command)
		print(f"  * snapshot {tag}", flush=True)
		print(f"    inspect it: docker run --rm -it {tag} bash", flush=True)

		return image_response(tag)

	def list_images(self) -> dict:
		return list_images(urlsplit(self.path).query)

	def get_image(self, image_id: str) -> dict:
		return image_response(image_id)

	def delete_image(self, image_id: str) -> dict:
		"""Atlas archives an image a machine still uses, so this never refuses either."""
		result = subprocess.run(["docker", "image", "rm", "-f", image_id], capture_output=True, text=True)
		if result.returncode != 0 and "No such image" in result.stderr:
			raise KeyError(image_id)

		print(f"  - image {image_id} deleted", flush=True)

		return {"id": image_id, "status": "Deleting"}

	def terminate(self, vm_id: str) -> dict:
		vm = VMS.get(vm_id)
		if vm and vm["container"]:
			subprocess.run(["docker", "rm", "-f", vm["container"]], capture_output=True)
			print(f"  - {vm_id} destroyed", flush=True)
		if vm:
			remove_vm_metadata(vm)
		with LOCK:
			VMS.pop(vm_id, None)
		write_ssh_config()

		return {"id": vm_id, "current_state": "terminating"}


def main() -> None:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--port", type=int, default=8100)
	parser.add_argument(
		"--boot-delay",
		type=int,
		default=BOOT_DELAY,
		help="seconds a machine stays Pending, so the scheduler sweep is exercised",
	)
	parser.add_argument(
		"--systemd",
		action="store_true",
		help="run containers under /sbin/init --privileged, so pilot's installer can start services",
	)
	args = parser.parse_args()

	Handler.systemd = args.systemd
	Handler.boot_delay = args.boot_delay
	print(
		f"fake atlas on http://127.0.0.1:{args.port}  (systemd={args.systemd}, boot delay={args.boot_delay}s)"
	)
	print("point Cargo Settings' Atlas URL at it, then build an Image")
	names = " ".join(f"{HOST_PREFIX}{slot}" for slot in range(1, SLOTS + 1))
	print(f"\nadd this line to /etc/hosts once, so the machine names resolve:\n127.0.0.1 {names}\n")
	ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
	main()
