# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class BucketCredential(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		access_key: DF.Data | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		secret_access_key: DF.Password | None
	# end: auto-generated types

	pass
