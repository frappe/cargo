# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class ObjectStorageNode(Document):
	"""One machine of a cluster, and what it does there."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		machine: DF.Link
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		role: DF.Literal["storage", "gateway"]
	# end: auto-generated types

	pass
