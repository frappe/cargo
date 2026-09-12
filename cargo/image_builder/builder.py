import subprocess
import time
from collections.abc import Callable

import frappe

from cargo.atlas_client import AtlasClient, base_image_id
from cargo.ssh import SshError, run_over_ssh, script

# Atlas records the snapshotted machine's shape as the warm-start template, so this is the
# shape a baked image boots at, not only the shape it bakes on. The bake outgrows the memory
# on its own, which is why the provision script runs on temporary swap.
BUILD_VCPUS = 1
BUILD_MEMORY_MIB = 1024
BUILD_DISK_MIB = 8 * 1024
PROVISION_SCRIPT = ("image_builder", "conf", "pilot", "provision.sh")
PROVISION_TIMEOUT = 3600
# Atlas reports a machine running once it is created, which is before it has booted. It
# answers the network first and sshd some time after that.
PING_TIMEOUT = 60
PING_INTERVAL = 2
SSH_READY_TIMEOUT = 180
SSH_READY_INTERVAL = 5
SSH_PROBE_TIMEOUT = 15


class Builder:
	"""Rents a machine, runs one script on it, photographs it, throws it away."""

	def __init__(self, atlas_name: str) -> None:
		self.atlas_name = atlas_name

	@property
	def client(self) -> AtlasClient:
		return AtlasClient.from_settings()

	def wait_until_reachable(self, address: str, private_key: str) -> None:
		"""Wait for the machine to answer, on the network first and then on SSH."""
		if not self.is_answering_ping(address):
			frappe.throw(
				frappe._("{0} did not answer a ping within {1} seconds.").format(address, PING_TIMEOUT)
			)

		if not self.is_accepting_ssh(address, private_key):
			frappe.throw(
				frappe._("{0} answered a ping but not SSH within {1} seconds.").format(
					address, SSH_READY_TIMEOUT
				)
			)

	def is_answering_ping(self, address: str) -> bool:
		"""Whether the machine reached the mesh before the deadline."""
		deadline = time.monotonic() + PING_TIMEOUT
		while True:
			reply = subprocess.run(
				["ping", "-6", "-c", "1", "-W", "2", address], capture_output=True, check=False
			)
			if reply.returncode == 0:
				return True

			if time.monotonic() >= deadline:
				return False

			time.sleep(PING_INTERVAL)

	def is_accepting_ssh(self, address: str, private_key: str) -> bool:
		"""Whether sshd answered a trivial command before the deadline.

		Only a failed command is retried. A missing ssh binary or an unusable key is not
		going to fix itself, and its own error says more than a readiness timeout."""
		deadline = time.monotonic() + SSH_READY_TIMEOUT
		while True:
			try:
				run_over_ssh(address, "uptime", private_key, timeout=SSH_PROBE_TIMEOUT)
				return True
			except SshError:
				if time.monotonic() >= deadline:
					return False

				time.sleep(SSH_READY_INTERVAL)

	def run_provision_script_on_build_machine(
		self,
		address: str,
		private_key: str,
		environment: dict[str, str],
		on_output: Callable[[str], None] | None = None,
	) -> str:
		"""Run the provision script on the machine."""
		return run_over_ssh(
			address,
			script(*PROVISION_SCRIPT, environment=environment),
			private_key,
			timeout=PROVISION_TIMEOUT,
			on_output=on_output,
		)

	def provision_build_machine(self, public_key: str) -> str:
		"""Cargo builder machines are ephemeral: they are created, provisioned, snapshotted, then destroyed."""
		return self.client.create_vm(
			image_id=base_image_id(),
			vcpus=BUILD_VCPUS,
			memory_mib=BUILD_MEMORY_MIB,
			disk_mib=BUILD_DISK_MIB,
			public_key=public_key,
			hostname=self.atlas_name,
		)["id"]

	def snapshot_build_machine(self, vm_id: str) -> str:
		"""Photograph the baked machine. This is the image. Both flags are what lets a host
		cache the artifacts and build a warm template, so a tenant VM starts from memory."""
		return self.client.create_snapshot(vm_id, self.atlas_name, cache_image=True, memory_snapshot=True)

	def destroy_build_machine(self, vm_id: str) -> bool:
		"""Best effort: a machine left running after a failed bake still costs money."""
		try:
			self.client.terminate_vm(vm_id)
		except Exception:
			frappe.log_error(title=f"Could not destroy build machine {vm_id}")
			return False

		return True
