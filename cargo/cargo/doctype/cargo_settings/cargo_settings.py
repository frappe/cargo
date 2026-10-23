# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from cargo.image_builder.doctype.pilot_image.pilot_image import start_base_images


class CargoSettings(Document):
	"""Where Cargo reaches Atlas, Proxy, and Central. Shared by every service."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from cargo.cargo.doctype.frappe_branch.frappe_branch import FrappeBranch
		from cargo.cargo.doctype.signup_app.signup_app import SignupApp

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
		first_storage_disk_growth_percent: DF.Percent
		frappe_branches: DF.Table[FrappeBranch]
		jwks_url: DF.Data
		max_auto_retry_count: DF.Int
		max_storage_node_disk: DF.Int
		metadata_snapshot_interval_hours: DF.Float
		proxy_token: DF.Password
		proxy_url: DF.Data
		region: DF.Data
		region_id: DF.Int
		require_prebuilt_assets: DF.Check
		signup_apps: DF.Table[SignupApp]
		storage_disk_min_hours_between_growth: DF.Float
		storage_disk_predict_hours: DF.Int
		storage_node_spread: DF.Literal["Separate hosts", "Off"]
		storage_node_spread_within: DF.Data | None
		track_pilot_releases: DF.Check
		update_pilot_in_base_image: DF.Check
		wildcard_domain: DF.Data
	# end: auto-generated types

	def validate(self) -> None:
		# Every consumer builds a domain from this, so it is canonicalised once, here.
		self.wildcard_domain = (self.wildcard_domain or "").strip().strip(".").lower()

	@frappe.whitelist()
	def build_base_images(self) -> list[str]:
		"""Start a Base image of the newest Pilot release on each Frappe branch head, if it has none."""
		return start_base_images(force=True)
