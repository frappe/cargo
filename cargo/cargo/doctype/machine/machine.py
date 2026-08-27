# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from __future__ import annotations

from typing import Any, Literal

from frappe.model.document import Document
from frappe.utils import now_datetime

MachineStatus = Literal["Pending", "Running", "Broken", "Terminated"]

# Atlas's statuses, not Cargo's.
DEAD_STATES = {"Failed", "Error", "Terminated", "Archived", "Broken"}


class Machine(Document):
	"""One VM Atlas created, for whichever service asked for it.

	Knows no service: `role` is free text, and the Atlas client is passed in.
	"""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		error: DF.SmallText | None
		ipv4_address: DF.Data | None
		last_synced_at: DF.Datetime | None
		reference_doctype: DF.Link
		reference_name: DF.DynamicLink
		role: DF.Data | None
		server: DF.Data | None
		status: DF.Literal["Pending", "Running", "Broken", "Terminated"]
		vm_id: DF.Data
		zone: DF.Data | None
	# end: auto-generated types

	def sync(self, client: Any) -> MachineStatus:
		"""Record this machine's state from Atlas.

		``client`` is anything with ``get_vm(vm_id) -> dict``; untyped to keep this free of
		service imports.
		"""
		self.last_synced_at = now_datetime()

		try:
			payload = client.get_vm(self.vm_id)
		except Exception as exception:
			return self.record(self.status, error=str(exception))

		status = payload.get("status")
		if status in DEAD_STATES:
			return self.record(
				"Terminated" if status == "Terminated" else "Broken",
				error=f"Atlas reported {status}",
			)

		# Running without an address is still booting, and cannot be configured yet.
		if status != "Running" or not payload.get("ipv4_address"):
			return self.record(self.status)

		self.ipv4_address = payload["ipv4_address"]
		self.server = payload.get("server")

		return self.record("Running")

	def record(self, status: MachineStatus, error: str | None = None) -> MachineStatus:
		"""Persist the state this sync found."""
		self.status = status
		self.error = error
		self.save(ignore_permissions=True)

		return self.status
