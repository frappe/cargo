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
		build_machine_cpu: DF.Int
		build_machine_disk_gb: DF.Int
		build_machine_memory: DF.Int
		cargo_url: DF.Data
		central_url: DF.Data
		central_webhook_enabled: DF.Check
		central_webhook_secret: DF.Password
		central_webhook_url: DF.Data | None
		jwks_url: DF.Data
		max_auto_retry_count: DF.Int
		proxy_token: DF.Password
		proxy_url: DF.Data
		region: DF.Data
		region_id: DF.Int
		track_pilot_releases: DF.Check
		version_supporting_app_toggle: DF.Data | None
		wildcard_domain: DF.Data
	# end: auto-generated types

	def validate(self) -> None:
		# Every consumer builds a domain from this, so it is canonicalised once, here.
		self.wildcard_domain = (self.wildcard_domain or "").strip().strip(".").lower()
