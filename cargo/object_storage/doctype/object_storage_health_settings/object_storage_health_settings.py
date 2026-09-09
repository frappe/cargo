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
		self.validate_percentages()
		self.validate_durations()

		if self.disk_critical_percent >= self.disk_degraded_percent:
			frappe.throw(
				_("Critical must be less free space than degraded, or nothing is ever degraded."),
				frappe.ValidationError,
			)

	def validate_percentages(self) -> None:
		"""Above 100 every disk is over the line; 0 is under none of them."""
		for fieldname in ("disk_degraded_percent", "disk_critical_percent"):
			if not 1 <= self.get(fieldname) <= 100:
				frappe.throw(
					_("{0} must be between 1 and 100.").format(_(self.meta.get_label(fieldname))),
					frappe.ValidationError,
				)

	def validate_durations(self) -> None:
		"""Zero is not "no wait" here: every call times out at once, every blink is an outage."""
		for fieldname in ("node_offline_seconds", "admin_timeout_seconds", "history_hours"):
			if self.get(fieldname) < 1:
				frappe.throw(
					_("{0} must be at least 1.").format(_(self.meta.get_label(fieldname))),
					frappe.ValidationError,
				)
