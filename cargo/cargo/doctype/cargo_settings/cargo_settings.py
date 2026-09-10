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

		atlas_key: DF.Data
		atlas_secret: DF.Password
		atlas_tenant_id: DF.Int
		atlas_url: DF.Data
		cargo_url: DF.Data
		central_url: DF.Data
		central_webhook_secret: DF.Password
		region: DF.Data
		region_id: DF.Int
	# end: auto-generated types
