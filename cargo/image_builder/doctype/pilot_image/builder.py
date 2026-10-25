import subprocess
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import frappe

from cargo.atlas_client import RUNNING_STATE, STOPPED_STATE, AtlasClient, AtlasError
from cargo.client_models import BUILDER, NodeSpec
from cargo.ssh import SshError, run_over_ssh, script

if TYPE_CHECKING:
	from cargo.cargo.doctype.machine.machine import Machine

PROVISION_SCRIPT = ("image_builder", "conf", "pilot", "provision.sh")
PROVISION_TIMEOUT = 3600
# Atlas reports a machine running once it is created, which is before it has booted. It
# answers the network first and sshd some time after that.
PING_TIMEOUT = 60
PING_INTERVAL = 2
SSH_READY_TIMEOUT = 180
SSH_READY_INTERVAL = 5
SSH_PROBE_TIMEOUT = 15
FLUSH_TIMEOUT = 300
STOP_TIMEOUT = 300
STOP_INTERVAL = 5


def get_build_spec() -> NodeSpec:
	"""The build machine's shape, from Cargo Settings. Atlas records the snapshotted machine's
	shape as the warm-start template, so this is also the shape a baked image boots at."""
	settings = frappe.get_cached_doc("Cargo Settings")
	return NodeSpec(
		role=BUILDER,
		cpu_millicores=settings.build_machine_cpu,
		ram_gb=settings.build_machine_memory,
		disk_gb=settings.build_machine_disk_gb,
	)


class Builder:
	"""Runs the provision script on a build machine. The machine itself is a `Machine`
	record, which is what rents it, photographs it and lets it go."""

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
			reply = subprocess.run(["ping", "-c", "1", "-W", "2", address], capture_output=True, check=False)
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

	def flush_build_machine(self, address: str, private_key: str) -> None:
		"""Flush buffered filesystem writes before shutdown."""
		run_over_ssh(address, "sync", private_key, timeout=FLUSH_TIMEOUT)

	def stop_build_machine(self, machine: "Machine") -> None:
		"""Flush and stop the builder before its disk is captured."""
		client = AtlasClient.from_settings()
		state = client.get_vm(machine.vm_id).get("current_state")
		if state != STOPPED_STATE:
			if state != RUNNING_STATE:
				raise AtlasError(f"Cannot prepare the build machine for capture while Atlas reports {state}.")
			self.flush_build_machine(machine.address, machine.get_password("ssh_private_key"))
		client.set_power_state(machine.vm_id, "stop")
		deadline = time.monotonic() + STOP_TIMEOUT
		while True:
			state = client.get_vm(machine.vm_id).get("current_state")
			if state == STOPPED_STATE:
				return
			if state != RUNNING_STATE:
				raise AtlasError(f"Build machine did not stop: Atlas reports {state}.")
			if time.monotonic() >= deadline:
				raise AtlasError(
					f"Build machine did not stop within {STOP_TIMEOUT} seconds. No image was captured."
				)
			time.sleep(STOP_INTERVAL)
