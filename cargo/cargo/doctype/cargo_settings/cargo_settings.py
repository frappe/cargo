# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CargoSettings(Document):
	"""Where Cargo reaches Atlas and Central. Shared by every service."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		atlas_access_token: DF.Password | None
		atlas_url: DF.Data
		central_access_token: DF.Password | None
		central_bootstrapping_token: DF.Password | None
		central_url: DF.Data
	# end: auto-generated types
