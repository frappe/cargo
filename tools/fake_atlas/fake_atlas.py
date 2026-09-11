#!/usr/bin/env python3
"""A stand-in for Atlas that hands out Docker containers instead of VMs.

Speaks the shape Cargo expects -- Atlas's tenant API under /api/atlas, an
`Authorization: token <key>:<secret>` header and an X-Tenant-ID header -- so nothing in the
Cargo app changes. Point Cargo Settings' Atlas URL at this and build an image for real.

    python3 fake_atlas.py --port 8100

Machines are named cargo-vm1, cargo-vm2 ... and Cargo is handed that name as the machine's
address, in the field real Atlas puts a mesh address in. Point every name at 127.0.0.1 in /etc/hosts once (the banner prints the line) and
HTTP, ssh and Garage's own peering all resolve without the host routing to container IPs,
which macOS will not do.
"""

import argparse
import json
import os
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = "/api/atlas"
CONTAINER_PREFIX = "cargo-fake"
IMAGES = {"ubuntu-24.04": "ubuntu:24.04", "ubuntu-22.04": "ubuntu:22.04"}
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
# Stock ubuntu images have no /sbin/init, and the usual prebuilt systemd images are amd64
# only. Build one locally instead, so this works on whatever architecture the host is.
SYSTEMD_DOCKERFILE = """
FROM {base}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \
 && apt-get install -y -qq systemd systemd-sysv dbus openssh-server sudo curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/sshd \
 && systemctl enable ssh
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


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
	return subprocess.run(command, capture_output=True, text=True, check=True, **kwargs)


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
	tag = f"cargo-fake/systemd-{base.replace(':', '-')}"
	if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0:
		return tag

	print(f"  building {tag} (first use, takes a minute)", flush=True)
	run(["docker", "build", "-t", tag, "-"], input=SYSTEMD_DOCKERFILE.format(base=base))

	return tag


def allocate_vm(vm_id: str) -> dict:
	"""Claim the lowest free slot, which fixes the machine's name and both its ports. Claimed
	under the lock: two concurrent requests would otherwise pick the same one."""
	with LOCK:
		taken = {vm["slot"] for vm in VMS.values()}
		slot = 1
		while slot in taken:
			slot += 1

		VMS[vm_id] = {
			"state": "pending",
			"slot": slot,
			"address": f"{HOST_PREFIX}{slot}",
			"port": FIRST_PORT + slot - 1,
			"container": None,
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
	vm_id: str, machine: dict, role: str, image: str, public_key: str, systemd: bool, delay: int
) -> None:
	"""Start the container and get sshd listening, then mark it Running.

	Stays Pending for `delay` seconds first, so Cargo has to come back for it."""
	name = f"{CONTAINER_PREFIX}-{vm_id}"
	host, port = machine["address"], machine["port"]
	command = [
		"docker",
		"run",
		"-d",
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
	if role == GATEWAY:
		command += ["-p", f"127.0.0.1:{ADMIN_PORT}:{ADMIN_PORT}"]
	if systemd:
		command += ["--privileged", "--cgroupns=host", "-v", "/sys/fs/cgroup:/sys/fs/cgroup:rw"]
	command += [image] + (["/sbin/init"] if systemd else ["sleep", "infinity"])

	try:
		ensure_network()
		run(command)
		with LOCK:
			VMS[vm_id]["container"] = name
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
	except subprocess.CalledProcessError as exception:
		with LOCK:
			VMS[vm_id].update(state="failed", error=(exception.stderr or "")[:500])
		print(f"  x {vm_id} broke: {(exception.stderr or '').strip()[:200]}", flush=True)
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
		return self.path[len(PREFIX) :].strip("/").split("/")

	def authenticated(self) -> bool:
		if not self.path.startswith(PREFIX):
			self.fail("unknown endpoint", 404, "not_found")
			return False
		if not (self.headers.get("Authorization") or "").startswith("token "):
			self.fail("Authorization: token <key>:<secret> required", 401, "authentication_required")
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
		except Exception as exception:
			self.fail(str(exception), 500, "internal_error")

	def do_GET(self) -> None:
		if not self.authenticated():
			return

		route = self.route
		try:
			if len(route) == 2 and route[0] == "virtual-machines":
				self.reply(self.get_virtual_machine(route[1]))
			elif len(route) == 2 and route[0] == "images":
				self.reply(self.get_image(route[1]))
			else:
				self.fail(f"unimplemented: GET {self.path}", 404, "not_found")
		except KeyError:
			self.fail("The resource does not exist.", 404, "not_found")
		except Exception as exception:
			self.fail(str(exception), 500, "internal_error")

	def do_DELETE(self) -> None:
		if not self.authenticated():
			return

		route = self.route
		if len(route) == 2 and route[0] == "virtual-machines":
			self.reply(self.terminate(route[1]), 202)
		else:
			self.fail(f"unimplemented: DELETE {self.path}", 404, "not_found")

	def create_virtual_machine(self, payload: dict) -> dict:
		"""One container per machine. Returns immediately: Cargo polls for running."""
		role = (payload.get("metadata") or {}).get("role")
		image = IMAGES.get(payload.get("image_id"), "ubuntu:24.04")
		if self.systemd:
			image = systemd_image(image)

		vm_id = f"vm-{uuid.uuid4().hex[:8]}"
		machine = allocate_vm(vm_id)
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
			"image_id": "ubuntu-24.04",
			"vcpus": 1,
			"memory_mib": 1024,
			"disk_mib": 10240,
			"created_at": int(time.time()),
			"current_state": vm["state"],
			"desired_state": "running",
			# Real Atlas reports a mesh address; here it is the slot name /etc/hosts knows.
			"wireguard_mesh_ipv6": vm["address"],
		}

	def get_virtual_machine(self, vm_id: str) -> dict:
		return self.as_response(vm_id)

	def create_snapshot(self, vm_id: str, payload: dict) -> dict:
		"""docker commit is the honest analogue of a disk snapshot."""
		vm = VMS[vm_id]
		tag = f"cargo-snapshot/{payload.get('title', vm_id)}".lower()
		run(["docker", "commit", vm["container"], tag])
		print(f"  * snapshot {tag}", flush=True)
		print(f"    inspect it: docker run --rm -it {tag} bash", flush=True)

		return {"id": tag, "title": payload.get("title", vm_id), "status": "Available"}

	def get_image(self, image_id: str) -> dict:
		result = subprocess.run(
			["docker", "image", "inspect", image_id, "--format", "{{.Size}}"],
			capture_output=True,
			text=True,
		)
		if result.returncode != 0:
			raise KeyError(image_id)

		return {
			"id": image_id,
			"status": "Available",
			"transfer_progress": 100,
			"transfer_error": None,
			"rootfs_size_mib": int(result.stdout.strip()) // (1024 * 1024),
		}

	def terminate(self, vm_id: str) -> dict:
		vm = VMS.get(vm_id)
		if vm and vm["container"]:
			subprocess.run(["docker", "rm", "-f", vm["container"]], capture_output=True)
			print(f"  - {vm_id} destroyed", flush=True)
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
	print("point Cargo Settings' Atlas URL at it, then build an Image Variant")
	names = " ".join(f"{HOST_PREFIX}{slot}" for slot in range(1, SLOTS + 1))
	print(f"\nadd this line to /etc/hosts once, so the machine names resolve:\n127.0.0.1 {names}\n")
	ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
	main()
