# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from cargo.atlas_client import DEAD_STATES, MIB_PER_GB, RUNNING_STATE, AtlasNotFound
from cargo.client_models import NodeSpec
from cargo.ssh import create_keypair

if TYPE_CHECKING:
	from cargo.atlas_client import AtlasClient

MachineStatus = Literal["Draft", "Pending", "Running", "Broken", "Terminated"]
DEAD_MACHINE_STATES = ("Broken", "Terminated")


class Machine(Document):
	"""One VM Atlas created, for whichever service asked for it."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		disk_size_gb: DF.Int
		error: DF.SmallText | None
		address: DF.Data | None
		last_synced_at: DF.Datetime | None
		reference_doctype: DF.Link
		reference_name: DF.DynamicLink
		role: DF.Data
		ssh_private_key: DF.Password | None
		ssh_public_key: DF.SmallText | None
		status: DF.Literal["Draft", "Pending", "Running", "Broken", "Terminated"]
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
				"disk_size_gb": spec.disk_gb,
				"zone": zone,
				"status": "Draft",
				"ssh_public_key": public_key,
				"ssh_private_key": private_key,
			}
		).insert(ignore_permissions=True)

		machine.assign(machine.build(spec, base_image))

		return machine

	def build(self, spec: NodeSpec, base_image: str) -> dict[str, str]:
		"""Ask Atlas to build this machine, and work out where it will answer.

		Nothing here holds a public address: machines are reached over the WG mesh, whose
		addresses Atlas derives rather than allocates, so one is known before it boots."""
		from cargo.atlas_client import AtlasClient

		client = AtlasClient.from_settings()
		try:
			created = client.create_vm(
				image_id=base_image,
				vcpus=spec.cpu,
				memory_mib=spec.ram_gb * MIB_PER_GB,
				disk_mib=spec.disk_gb * MIB_PER_GB,
				public_key=self.ssh_public_key,
				hostname=self.name,
				metadata={"role": self.role},
			)
		except Exception:
			frappe.log_error(
				title=f"{self.reference_name} could not add a {self.role} machine",
				message=frappe.get_traceback(with_context=True),
			)
			frappe.throw(_("Atlas would not build this machine. See the Error Log."))

		return {"vm_id": created["id"], "address": client.address_of(created)}

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

	def assign(self, built: dict[str, str]) -> MachineStatus:
		"""Atlas built this machine: it has an id and an address now, and is booting."""
		self.vm_id = built["vm_id"]
		self.address = built["address"]

		return self.record("Pending")

	def sync(self, client: AtlasClient) -> MachineStatus:
		"""Record this machine's state from Atlas. A machine Atlas no longer has is one
		whose termination finished, so 404 is an answer rather than a failure."""
		self.last_synced_at = now_datetime()

		try:
			payload = client.get_vm(self.vm_id)
		except AtlasNotFound:
			return self.record("Terminated", error="Atlas no longer has this machine")
		except Exception as exception:
			return self.record(self.status, error=str(exception))

		state = payload.get("current_state")
		if state in DEAD_STATES:
			return self.record("Broken", error=f"Atlas reported {state}")

		if state != RUNNING_STATE:
			return self.record(self.status)

		return self.record("Running")

	def record(self, status: MachineStatus, error: str | None = None) -> MachineStatus:
		self.status = status
		self.error = error
		self.save(ignore_permissions=True)

		return self.status


def sync_pending_machines() -> None:
	"""Refresh every machine Atlas is still building, then let each owner react."""
	from cargo.atlas_client import AtlasClient

	client = AtlasClient.from_settings()
	settled: set[tuple[str, str]] = set()

	for name in frappe.get_all("Machine", filters={"status": "Pending"}, pluck="name"):
		machine: Machine = frappe.get_doc("Machine", name)
		if machine.sync(client) != "Pending":
			settled.add((machine.reference_doctype, machine.reference_name))

	# Only owners whose machines actually moved: the rest have nothing new to judge.
	for doctype, name in settled:
		try:
			frappe.get_doc(doctype, name).sync_machines()
		except Exception:
			frappe.log_error(title=f"{name} could not take its machines' new state")
