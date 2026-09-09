# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

PUBLIC_KEY_FILE = "datum.pub"


class DatumSettings(Document):
	"""What a datum host runs on. These become datum's environment, nothing else reads them."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		clickhouse_host: DF.Data
		clickhouse_password: DF.Password | None
		clickhouse_port: DF.Int
		clickhouse_user: DF.Data | None
		oidc_issuer: DF.Data | None
		public_key: DF.Code | None
		repository: DF.Data | None
		timeout_seconds: DF.Int
		version: DF.Data | None
	# end: auto-generated types

	def validate(self) -> None:
		self.oidc_issuer = (self.oidc_issuer or "").strip().rstrip("/") or None
		if not (self.oidc_issuer or self.public_key):
			frappe.throw(
				_("Set an OIDC issuer or a public key, or datum will answer 401 to everything."),
				frappe.ValidationError,
			)

	def environment(self, key_file: str = PUBLIC_KEY_FILE) -> dict[str, str]:
		"""Datum's environment. `key_file` is where the PEM was written on that host."""
		variables = {
			"DATUM_CLICKHOUSE_HOST": self.clickhouse_host,
			"DATUM_CLICKHOUSE_PORT": str(self.clickhouse_port),
			"DATUM_CLICKHOUSE_USER": self.clickhouse_user,
			"DATUM_CLICKHOUSE_PASSWORD": self.get_password("clickhouse_password", raise_exception=False),
			"DATUM_TIMEOUT": str(self.timeout_seconds),
			"DATUM_OIDC_ISSUER": self.oidc_issuer,
			"DATUM_JWT_PUBLIC_KEY_FILE": key_file if self.public_key else None,
		}

		return {name: value for name, value in variables.items() if value}
