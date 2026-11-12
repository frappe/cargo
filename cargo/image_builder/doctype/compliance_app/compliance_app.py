# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class ComplianceApp(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		compliance_apps: DF.Data | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		signup_app: DF.Data | None
	# end: auto-generated types

	pass
