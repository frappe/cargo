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
		try:
			payload = client.get_vm(self.vm_id)
		except Exception as exception:
			self.db_set({"last_synced_at": now_datetime(), "error": str(exception)})
			return self.status

		status = payload.get("status")
		if status in DEAD_STATES:
			self.db_set(
				{
					"status": "Terminated" if status == "Terminated" else "Broken",
					"last_synced_at": now_datetime(),
					"error": f"Atlas reported {status}",
				}
			)
			return self.status

		# Running without an address is still booting, and cannot be configured yet.
		if status != "Running" or not payload.get("ipv4_address"):
			self.db_set({"last_synced_at": now_datetime(), "error": None})
			return self.status

		self.db_set(
			{
				"status": "Running",
				"ipv4_address": payload["ipv4_address"],
				"server": payload.get("server"),
				"last_synced_at": now_datetime(),
				"error": None,
			}
		)

		return self.status
