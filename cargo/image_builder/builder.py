import shlex
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

import frappe
from frappe import _

from cargo.atlas_client import AtlasClient
from cargo.ssh import run_over_ssh

BASE_IMAGE = "ubuntu-24.04"
# A new kind is a new conf/<kind>/provision.sh and an entry here.
KINDS = ("pilot",)
PROVISION_TIMEOUT = 3600
# Atlas will be responsible for placing machine's identity on boot from snapshot.
WIPE_IDENTITY = """
set -e
truncate -s 0 /etc/machine-id
rm -f /root/.ssh/authorized_keys /etc/ssh/ssh_host_*
sync
"""


class Builder:
	"""Rents a machine, runs one script on it, photographs it, throws it away."""

	def __init__(self, kind: str, atlas_name: str) -> None:
		if kind not in KINDS:
			frappe.throw(_("{0} is not an image Cargo knows how to build.").format(kind))

		self.kind = kind
		self.atlas_name = atlas_name

	@property
	def client(self) -> AtlasClient:
		return AtlasClient.from_settings()

	def wipe_machine_identity(self, address: str, private_key: str) -> str:
		"""Take the build key and this machine's identity off the disk. Runs last."""
		return run_over_ssh(address, WIPE_IDENTITY, private_key, timeout=PROVISION_TIMEOUT)

	def provision_script(self, environment: dict[str, str]) -> str:
		"""This kind's script, with the image's arguments exported ahead of it."""
		exports = "\n".join(f"export {key}={shlex.quote(value)}" for key, value in environment.items())
		script = Path(
			frappe.get_app_path("cargo", "image_builder", "conf", self.kind, "provision.sh")
		).read_text()

		return f"{exports}\n{script}"

	def run_provision_script_on_build_machine(
		self,
		address: str,
		private_key: str,
		environment: dict[str, str],
		on_output: Callable[[str], None] | None = None,
	) -> str:
		"""Run this kind's script on the machine."""
		return run_over_ssh(
			address,
			self.provision_script(environment),
			private_key,
			timeout=PROVISION_TIMEOUT,
			on_output=on_output,
		)

	def provision_build_machine(self, public_key: str) -> str:
		"""Cargo builder machines are ephemeral: they are created, provisioned, snapshotted, then destroyed."""
		vm_ids = self.client.create_vms(self.atlas_name, public_key=public_key, base_image=BASE_IMAGE)

		return vm_ids[0]

	def snapshot_build_machine(self, vm_id: str) -> str:
		"""Photograph the baked machine. This is the image."""
		return self.client.create_snapshot(vm_id, self.atlas_name)

	def destroy_build_machine(self, vm_id: str) -> bool:
		"""Best effort: a machine left running after a failed bake still costs money."""
		try:
			self.client.terminate_vm(vm_id)
		except Exception:
			frappe.log_error(title=f"Could not destroy build machine {vm_id}")
			return False

		return True
