#!/usr/bin/env python3
"""A stand-in for Atlas that hands out Docker containers instead of VMs.

Speaks the same shape Cargo expects -- POST /api/method/atlas.atlas.api.service.<name>,
X-Cargo-Token header, {"message": ...} bodies -- so nothing in the Cargo app changes.
Point Cargo Settings' Atlas URL at this and build an image for real.

    python3 fake_atlas.py --port 8100

Machines are named cargo-vm1, cargo-vm2 ... and Cargo is handed that name as the machine's
address. Point every name at 127.0.0.1 in /etc/hosts once (the banner prints the line) and
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

PREFIX = "/api/method/atlas.atlas.api.service."
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
			"status": "Pending",
			"slot": slot,
			"ipv4_address": f"{HOST_PREFIX}{slot}",
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
			f"Host {vm['ipv4_address']}\n"
			f"\tHostName 127.0.0.1\n"
			f"\tPort {vm['port']}\n"
			f"\tUser root\n"
			f"\tStrictHostKeyChecking no\n"
			f"\tUserKnownHostsFile /dev/null\n"
			for vm in VMS.values()
			if vm["ipv4_address"] and (live is None or vm["container"] in live)
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
	host, port = machine["ipv4_address"], machine["port"]
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
			VMS[vm_id].update(status="Broken", error=(exception.stderr or "")[:500])
		print(f"  x {vm_id} broke: {(exception.stderr or '').strip()[:200]}", flush=True)
		return

	with LOCK:
		VMS[vm_id].update(status="Running")
	admin = f", admin {ADMIN_PORT}" if role == GATEWAY else ""
	print(f"  + {vm_id} Running -> {host} (ssh {port}{admin})", flush=True)


class Handler(BaseHTTPRequestHandler):
	systemd = False
	boot_delay = BOOT_DELAY

	def log_message(self, *args) -> None:
		pass

	def reply(self, payload, status: int = 200) -> None:
		body = json.dumps({"message": payload}).encode()
		self.send_response(status)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def do_POST(self) -> None:
		if not self.path.startswith(PREFIX):
			self.reply({"error": "unknown endpoint"}, 404)
			return
		if not self.headers.get("X-Cargo-Token"):
			self.reply({"error": "X-Cargo-Token required"}, 401)
			return

		length = int(self.headers.get("Content-Length") or 0)
		params = json.loads(self.rfile.read(length) or "{}")
		method = self.path[len(PREFIX) :]
		print(
			f"-> {method} {params.get('title') or params.get('name') or params.get('vm') or ''}", flush=True
		)

		handler = getattr(self, f"atlas_{method}", None)
		if not handler:
			self.reply({"error": f"unimplemented: {method}"}, 400)
			return
		try:
			self.reply(handler(params))
		except Exception as exception:
			self.reply({"error": str(exception)}, 500)

	def atlas_create_bare_vms(self, params: dict) -> dict:
		"""One container per machine. Returns immediately: Cargo polls for Running."""
		# One machine per spec `count`, in spec order: Cargo decides which machine is the
		# gateway purely by position in the returned list.
		specs = (params.get("placement_group") or {}).get("specs") or []
		roles = [spec.get("role") for spec in specs for _ in range(int(spec.get("count", 1)))] or [None]
		image = IMAGES.get(params.get("base_image"), "ubuntu:24.04")
		if self.systemd:
			image = systemd_image(image)
		vm_ids = []

		for role in roles:
			vm_id = f"vm-{uuid.uuid4().hex[:8]}"
			machine = allocate_vm(vm_id)
			write_ssh_config()
			threading.Thread(
				target=boot,
				args=(
					vm_id,
					machine,
					role,
					image,
					params.get("ssh_public_key", ""),
					self.systemd,
					self.boot_delay,
				),
				daemon=True,
			).start()
			vm_ids.append(vm_id)

		return {"vm_ids": vm_ids}

	def atlas_get_virtual_machine(self, params: dict) -> dict:
		vm = VMS.get(params.get("name") or params.get("vm"))
		if not vm:
			return {"status": "Terminated"}

		return {"status": vm["status"], "ipv4_address": vm["ipv4_address"], "server": vm["container"]}

	def atlas_create_snapshot(self, params: dict) -> dict:
		"""docker commit is the honest analogue of a disk snapshot."""
		vm = VMS[params["vm"]]
		tag = f"cargo-snapshot/{params.get('title', params['vm'])}".lower()
		run(["docker", "commit", vm["container"], tag])
		image_id = run(["docker", "image", "inspect", tag, "--format", "{{.Id}}"]).stdout.strip()
		print(f"  * snapshot {tag} ({image_id[:19]})", flush=True)
		print(f"    inspect it: docker run --rm -it {tag} bash", flush=True)

		return {"snapshot_id": tag, "image_id": image_id}

	def atlas_get_snapshot(self, params: dict) -> dict:
		snapshot = params.get("snapshot")
		result = subprocess.run(
			["docker", "image", "inspect", snapshot, "--format", "{{.Id}} {{.Size}}"],
			capture_output=True,
			text=True,
		)
		if result.returncode != 0:
			return {"status": "Missing"}
		image_id, size = result.stdout.split()

		return {"status": "Available", "snapshot_id": snapshot, "image_id": image_id, "size": int(size)}

	def atlas_terminate_vm(self, params: dict) -> dict:
		vm_id = params.get("vm") or params.get("name")
		vm = VMS.get(vm_id)
		if vm and vm["container"]:
			subprocess.run(["docker", "rm", "-f", vm["container"]], capture_output=True)
			print(f"  - {vm_id} destroyed", flush=True)
		with LOCK:
			VMS.pop(vm_id, None)
		write_ssh_config()

		return {"status": "Terminated"}


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
