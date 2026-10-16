# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from cargo.atlas_client import (
	DEAD_STATES,
	MIB_PER_GB,
	PAUSED_STATE,
	RUNNING_STATE,
	STOPPED_STATE,
	AtlasError,
	AtlasNotFound,
)
from cargo.client_models import NodeSpec, PlacementRule
from cargo.ssh import create_keypair

if TYPE_CHECKING:
	from cargo.atlas_client import AtlasClient

MachineStatus = Literal["Draft", "Pending", "Running", "Stopped", "Paused", "Broken", "Terminated"]
DEAD_MACHINE_STATES = ("Broken", "Terminated")
POWER_ACTIONS = ("start", "stop", "pause", "resume", "restart")


class Machine(Document):
	"""One VM Atlas created, for whichever service asked for it."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		address: DF.Data | None
		cpu_millicores: DF.Int
		disk_size_gb: DF.Int
		error: DF.SmallText | None
		last_synced_at: DF.Datetime | None
		ram_gb: DF.Int
		reference_doctype: DF.Link
		reference_name: DF.DynamicLink
		role: DF.Data
		ssh_private_key: DF.Password | None
		ssh_public_key: DF.SmallText | None
		status: DF.Literal["Draft", "Pending", "Running", "Stopped", "Paused", "Broken", "Terminated"]
		vm_id: DF.Data | None
		zone: DF.Data | None
	# end: auto-generated types

	def before_insert(self) -> None:
		"""Its own keypair, unless the service handed one down: a machine can only ever be
		reached with the key it was built with."""
		if not self.ssh_public_key:
			self.ssh_public_key, self.ssh_private_key = create_keypair(self.reference_name)

	@classmethod
	def request(
		cls,
		owner: Document,
		spec: NodeSpec,
		*,
		base_image: str,
		zone: str = "",
		ssh_keypair: tuple[str, str] | None = None,
		tags: dict[str, str] | None = None,
		placement_rules: list[PlacementRule] | None = None,
	) -> Machine:
		"""Record a machine and ask Atlas to build it, returning the row. Throws with the
		row rolled back, so nothing is left claiming a VM that was never made."""
		public_key, private_key = ssh_keypair or (None, None)
		machine: Machine = frappe.get_doc(
			{
				"doctype": "Machine",
				"reference_doctype": owner.doctype,
				"reference_name": owner.name,
				"role": spec.role,
				"cpu_millicores": spec.cpu_millicores,
				"ram_gb": spec.ram_gb,
				"disk_size_gb": spec.disk_gb,
				"zone": zone,
				"status": "Draft",
				"ssh_public_key": public_key,
				"ssh_private_key": private_key,
			}
		).insert(ignore_permissions=True)

		machine.vm_id = machine.build(spec, base_image, tags=tags, placement_rules=placement_rules)
		machine.record("Pending")
		return machine

	def build(
		self,
		spec: NodeSpec,
		base_image: str,
		tags: dict[str, str] | None = None,
		placement_rules: list[PlacementRule] | None = None,
	) -> str:
		"""Ask Atlas to build this machine, and return the id it goes by."""
		from cargo.atlas_client import AtlasClient

		client = AtlasClient.from_settings()
		try:
			created = client.create_vm(
				image_id=base_image,
				cpu_millicores=spec.cpu_millicores,
				memory_mib=spec.ram_gb * MIB_PER_GB,
				disk_mib=spec.disk_gb * MIB_PER_GB,
				public_key=self.ssh_public_key,
				hostname=self.name,
				metadata={"role": self.role},
				tags=tags,
				placement_rules=placement_rules,
			)
		except Exception:
			frappe.log_error(
				title=f"{self.reference_name} could not add a {self.role} machine",
				message=frappe.get_traceback(with_context=True),
			)
			frappe.throw(_("Atlas would not build this machine. See the Error Log."))

		return created["id"]

	def snapshot(
		self,
		title: str,
		tags: dict[str, str],
		image_type: str = "system",
		memory_snapshot: bool = True,
		cache_image: bool = True,
	) -> str:
		"""Photograph this machine and return the image Atlas filed it under. Cached and
		memory-backed, so a machine booted from it starts warm."""
		from cargo.atlas_client import AtlasClient

		return AtlasClient.from_settings().create_snapshot(
			self.vm_id,
			title,
			image_type=image_type,
			cache_image=cache_image,
			memory_snapshot=memory_snapshot,
			tags=tags,
		)

	def terminate(self) -> bool:
		"""Tell Atlas to let this machine go. A refusal leaves it Broken rather than
		pretending it is gone, since it is still running and still costing money."""
		from cargo.atlas_client import AtlasClient

		try:
			AtlasClient.from_settings().terminate_vm(self.vm_id)
		except Exception:
			frappe.log_error(
				title=f"Could not terminate {self.role} machine {self.name}",
				message=frappe.get_traceback(with_context=True),
			)
			self.record("Broken")
			return False

		self.record("Terminated")

		return True

	def sync(self, client: AtlasClient) -> MachineStatus:
		"""Record this machine's state, address and size from Atlas. Saves only on a change,
		so an open form is not outdated every minute."""
		before = self.synced_values
		self.status, self.error = self.get_atlas_status(client)
		self.last_synced_at = now_datetime()

		if self.synced_values == before:
			self.db_set("last_synced_at", self.last_synced_at, update_modified=False)
		else:
			self.save(ignore_permissions=True)

		return self.status

	@property
	def synced_values(self) -> tuple:
		return (self.status, self.error, self.address, self.cpu_millicores, self.ram_gb, self.disk_size_gb)

	def get_atlas_status(self, client: AtlasClient) -> tuple[MachineStatus, str | None]:
		"""The status and error Atlas's state maps to. A machine Atlas no longer has is one whose
		termination finished, so 404 is an answer rather than a failure."""
		try:
			payload = client.get_vm(self.vm_id)
		except AtlasNotFound:
			return "Terminated", "Atlas no longer has this machine"
		except Exception as exception:
			return self.status, str(exception)

		if compute := payload.get("compute"):
			self.cpu_millicores = compute["cpu_millicores"]
			self.ram_gb = compute["memory_mib"] // MIB_PER_GB
		if disk := payload.get("disk"):
			self.disk_size_gb = disk["size_mib"] // MIB_PER_GB

		state = payload.get("current_state")
		if state in DEAD_STATES:
			return "Broken", f"Atlas reported {state}: {payload.get('error') or _('no reason given')}"
		if state == STOPPED_STATE:
			return "Stopped", None
		if state == PAUSED_STATE:
			return "Paused", None
		if state != RUNNING_STATE:
			return self.status, None

		self.address = payload.get("network", {}).get("mesh_ipv6")
		if not self.address:
			return "Broken", "Atlas reported no mesh address"

		return "Running", None

	@frappe.whitelist()
	def change_power_state(self, action: str) -> None:
		"""Start, stop, pause, resume or restart the VM in Atlas."""
		from cargo.atlas_client import AtlasClient

		self.check_permission("write")
		if action not in POWER_ACTIONS:
			frappe.throw(_("Unknown power action {0}.").format(action))

		try:
			AtlasClient.from_settings().set_power_state(self.vm_id, action)
		except AtlasError as exception:
			frappe.throw(_("Atlas would not {0} this machine: {1}").format(action, exception))

	@frappe.whitelist()
	def grow_disk(self, disk_gb: int) -> None:
		"""Ask Atlas to grow the disk without stopping the VM. The sync records the new size."""
		from cargo.atlas_client import AtlasClient

		self.check_permission("write")
		disk_gb = int(disk_gb)
		if disk_gb <= self.disk_size_gb:
			frappe.throw(_("The new disk size must be larger than {0} GB.").format(self.disk_size_gb))

		try:
			AtlasClient.from_settings().grow_disk(self.vm_id, disk_gb * MIB_PER_GB)
		except AtlasError as exception:
			frappe.throw(_("Atlas would not grow this machine's disk: {0}").format(exception))

	@frappe.whitelist()
	def resize_machine(self, cpu_millicores: int, ram_gb: int, disk_gb: int) -> None:
		"""Ask Atlas to resize the stopped VM. The disk only grows. The sync records the new size."""
		from cargo.atlas_client import AtlasClient

		self.check_permission("write")
		cpu_millicores, ram_gb, disk_gb = int(cpu_millicores), int(ram_gb), int(disk_gb)
		if disk_gb < self.disk_size_gb:
			frappe.throw(_("Disk size can only be increased, not decreased."))

		try:
			client = AtlasClient.from_settings()
			state = client.get_vm(self.vm_id).get("current_state")
			if state != STOPPED_STATE:
				frappe.throw(_("Stop the machine before resizing it. Atlas reports it {0}.").format(state))

			client.resize_vm(
				self.vm_id,
				cpu_millicores=cpu_millicores,
				memory_mib=ram_gb * MIB_PER_GB,
				disk_mib=disk_gb * MIB_PER_GB,
			)
		except AtlasError as exception:
			frappe.throw(_("Atlas would not resize this machine: {0}").format(exception))

	def record(self, status: MachineStatus, error: str | None = None) -> MachineStatus:
		self.status = status
		self.error = error
		self.save(ignore_permissions=True)

		return self.status


def sync_all_machines() -> None:
	"""Refresh every machine Atlas has not terminated, then let each owner whose machine
	changed status react."""
	from cargo.atlas_client import AtlasClient

	client = AtlasClient.from_settings()
	changed: set[tuple[str, str]] = set()
	names = frappe.get_all(
		"Machine", filters={"status": ("!=", "Terminated"), "vm_id": ("is", "set")}, pluck="name"
	)

	for name in names:
		machine: Machine = frappe.get_doc("Machine", name)
		before = machine.status
		if machine.sync(client) != before:
			changed.add((machine.reference_doctype, machine.reference_name))

	for doctype, name in changed:
		try:
			frappe.get_doc(doctype, name).sync_machines()
		except Exception:
			frappe.log_error(title=f"{name} could not take its machines' new state")
