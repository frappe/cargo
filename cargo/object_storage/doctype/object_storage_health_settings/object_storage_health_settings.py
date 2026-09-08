# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class ObjectStorageHealthSettings(Document):
	"""When a cluster counts as degraded, and when as critical. Region-wide: one cluster
	to a region, so a per-cluster override would never be set."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		admin_timeout_seconds: DF.Int
		disk_critical_percent: DF.Int
		disk_degraded_percent: DF.Int
		history_hours: DF.Int
		node_offline_seconds: DF.Int
	# end: auto-generated types

	def validate(self) -> None:
		if self.disk_critical_percent >= self.disk_degraded_percent:
			frappe.throw(
				_("Critical must be less free space than degraded, or nothing is ever degraded."),
				frappe.ValidationError,
			)
