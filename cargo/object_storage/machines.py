from __future__ import annotations

import typing

import frappe
from frappe import _

from cargo.atlas_client import AtlasClient
from cargo.object_storage.client_models import NodeSpec, Role

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.machine.machine import Machine, MachineStatus
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)

DEAD_STATES = ("Broken", "Terminated")


class MachineFleet:
	"""The machines one cluster runs on: asking Atlas for them, and keeping their state fresh."""

	def __init__(self, cluster: ObjectStorageCluster) -> None:
		self.cluster = cluster

	@property
	def names(self) -> list[str]:
		return self.cluster.associated_machines

	@property
	def vm_ids(self) -> list[str]:
		return frappe.get_all("Machine", filters={"name": ("in", self.names)}, pluck="vm_id")

	def in_status(self, status: MachineStatus) -> list[str]:
		if not self.names:
			return []

		return frappe.get_all("Machine", filters={"name": ("in", self.names), "status": status}, pluck="name")

	def request(self, role: Role, cpu: int, ram_gb: int, disk_gb: int) -> Machine:
		"""Record one machine and ask Atlas to build it. Throws, rolling the record back."""
		machine: Machine = frappe.get_doc(
			{
				"doctype": "Machine",
				"reference_doctype": self.cluster.doctype,
				"reference_name": self.cluster.name,
				"role": role,
				"disk_size_gb": disk_gb,
				"zone": self.cluster.region,
				"status": "Draft",
			}
		).insert(ignore_permissions=True)

		machine.assign(self.build(machine, cpu, ram_gb, disk_gb))

		return machine

	def build(self, machine: Machine, cpu: int, ram_gb: int, disk_gb: int) -> str:
		"""The VM id Atlas built for one machine."""
		specs = [NodeSpec(role=machine.role, count=1, cpu=cpu, ram_gb=ram_gb, disk_gb=disk_gb)]

		try:
			vm_ids = AtlasClient.from_settings().create_vms(
				title=f"{self.cluster.name} object storage",
				placement=self.cluster.placement_schema(specs),
				base_image=self.cluster.base_image,
				public_key=self.cluster.ssh_public_key,
			)
		except Exception:
			frappe.log_error(
				title=f"{self.cluster.name} could not add a {machine.role} machine",
				message=frappe.get_traceback(with_context=True),
			)
			frappe.throw(_("Atlas would not build this machine. See the Error Log."))

		if len(vm_ids) != 1:
			# Atlas cleans up what this cluster will not record.
			frappe.throw(_(f"Asked Atlas for one machine and got {len(vm_ids)}: {', '.join(vm_ids)}."))

		return vm_ids[0]

	def sync(self) -> list[MachineStatus]:
		"""Refresh the machines still booting, then say what state they all ended in."""
		client = AtlasClient.from_settings()
		states = []
		for name in self.in_status("Pending"):
			machine: Machine = frappe.get_doc("Machine", name)
			status = machine.sync(client)
			if status != "Draft":
				states.append(status)

		if not self.names:
			return []

		return states


def sync_pending_machines() -> None:
	"""Walk every cluster still waiting on Atlas. Scheduled in `hooks.py`."""
	waiting = frappe.get_all(
		"Machine",
		filters={"reference_doctype": "Object Storage Cluster", "status": "Pending"},
		pluck="reference_name",
		distinct=True,
	)
	for name in waiting:
		cluster: ObjectStorageCluster = frappe.get_doc("Object Storage Cluster", name)
		cluster.sync_machines()
