# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CargoSettings(Document):
	"""Where Cargo reaches Atlas, Proxy, and Central. Shared by every service."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		atlas_tenant_id: DF.Int
		atlas_token: DF.Password
		atlas_url: DF.Data
		cargo_url: DF.Data
		central_url: DF.Data
		central_webhook_secret: DF.Password
		jwks_url: DF.Data
		proxy_token: DF.Password
		proxy_url: DF.Data
		region: DF.Data
		region_id: DF.Int
		wildcard_domain: DF.Data
	# end: auto-generated types

	def validate(self) -> None:
		# Every consumer builds a domain from this, so it is canonicalised once, here.
		self.wildcard_domain = (self.wildcard_domain or "").strip().strip(".").lower()
