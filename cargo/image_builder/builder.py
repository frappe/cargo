from collections.abc import Callable

import frappe

from cargo.atlas_client import AtlasClient
from cargo.ssh import run_over_ssh, script

# Atlas names an image by a generated id, so the one to bake on is found by what it holds.
BASE_OPERATING_SYSTEM = "Ubuntu"
BASE_OPERATING_SYSTEM_VERSION = "24.04"
# Atlas records the snapshotted machine's shape as the warm-start template, so this is the
# shape a baked image boots at, not only the shape it bakes on. The bake outgrows the memory
# on its own, which is why the provision script runs on temporary swap.
BUILD_VCPUS = 1
BUILD_MEMORY_MIB = 1024
BUILD_DISK_MIB = 8 * 1024
PROVISION_SCRIPT = ("image_builder", "conf", "pilot", "provision.sh")
PROVISION_TIMEOUT = 3600


class Builder:
	"""Rents a machine, runs one script on it, photographs it, throws it away."""

	def __init__(self, atlas_name: str) -> None:
		self.atlas_name = atlas_name

	@property
	def client(self) -> AtlasClient:
		return AtlasClient.from_settings()

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
		client = self.client
		image_id = client.find_system_image(BASE_OPERATING_SYSTEM, BASE_OPERATING_SYSTEM_VERSION)
		if not image_id:
			frappe.throw(
				frappe._("Atlas has no available {0} {1} system image to build on.").format(
					BASE_OPERATING_SYSTEM, BASE_OPERATING_SYSTEM_VERSION
				)
			)

		return client.create_vm(
			image_id=image_id,
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
