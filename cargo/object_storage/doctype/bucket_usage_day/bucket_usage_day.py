# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class BucketUsageDay(Document):
	"""One bucket's peak bytes on one UTC day: Cargo's outbox of storage usage for Central."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		bucket_name: DF.Data
		confirmed_at: DF.Datetime | None
		confirmed_bytes: DF.LongInt
		day: DF.Date
		peak_bytes: DF.LongInt
		reason: DF.SmallText | None
		refused: DF.Check
		sampled_at: DF.Datetime | None
		sent_at: DF.Datetime | None
		sent_bytes: DF.LongInt
	# end: auto-generated types

	pass


def on_doctype_update():
	frappe.db.add_unique("Bucket Usage Day", ["bucket_name", "day"], constraint_name="unique_bucket_day")
