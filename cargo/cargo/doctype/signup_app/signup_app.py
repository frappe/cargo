# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SignupApp(Document):
	"""An app a new site can sign up with, built into Site and Apps images."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		app: DF.Data
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
	# end: auto-generated types

	pass
