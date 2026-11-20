# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from __future__ import annotations

import typing
from datetime import timedelta

import frappe
from cloudtoolkit.tls.letsencrypt import LetsEncrypt, create_account_key
from frappe import _
from frappe.model.document import Document
from frappe.utils import convert_utc_to_system_timezone, get_datetime, now_datetime

from cargo.atlas_client import AtlasClient

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings
	from cargo.nginx import Nginx

SETTINGS = "Cargo Settings"
RENEW_WITHIN_DAYS = 30


class ServiceCertificate(Document):
	"""A Let's Encrypt certificate for one service's domains, named after the service's nginx config."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		certificate: DF.LongText | None
		domains: DF.SmallText | None
		expires_on: DF.Datetime | None
		private_key: DF.Password | None
		status: DF.Literal["Pending", "Active", "Failed"]
	# end: auto-generated types

	@property
	def dns_names(self) -> list[str]:
		return (self.domains or "").split()

	@property
	def is_due(self) -> bool:
		"""Never issued, failed last time, or expires within `RENEW_WITHIN_DAYS`."""
		if self.status != "Active" or not self.expires_on:
			return True

		return get_datetime(self.expires_on) - now_datetime() < timedelta(days=RENEW_WITHIN_DAYS)

	def issue(self) -> None:
		"""Issue over dns-01, with the challenge records set through Atlas. A failure is kept as Failed."""
		try:
			issued = get_letsencrypt().issue_certificate(self.dns_names)
		except Exception:
			self.status = "Failed"
			self.save()
			raise

		self.certificate = issued.certificate_pem
		self.private_key = issued.private_key_pem
		self.expires_on = convert_utc_to_system_timezone(issued.expires_on).replace(tzinfo=None)
		self.status = "Active"
		self.save()


def get_service_certificate(name: str, domains: tuple[str, ...]) -> ServiceCertificate:
	"""The certificate for `name`, issued first when it is new, due, or names other domains."""
	if not frappe.db.exists("Service Certificate", name):
		certificate: ServiceCertificate = frappe.get_doc(
			{"doctype": "Service Certificate", "name": name, "domains": "\n".join(domains)}
		).insert()
	else:
		certificate: ServiceCertificate = frappe.get_doc("Service Certificate", name)
		if certificate.dns_names != list(domains):
			certificate.update({"domains": "\n".join(domains), "status": "Pending"})
			certificate.save()

	if certificate.is_due:
		certificate.issue()

	return certificate


def get_letsencrypt() -> LetsEncrypt:
	settings: CargoSettings = frappe.get_single(SETTINGS)
	return LetsEncrypt(
		get_account_key(settings),
		settings.letsencrypt_email,
		AtlasClient.from_settings(),
		is_staging=bool(settings.is_letsencrypt_staging),
	)


def get_account_key(settings: CargoSettings) -> str:
	"""The ACME account key, created and stored the first time a certificate is issued."""
	account_key = settings.get_password("letsencrypt_account_key", raise_exception=False)
	if account_key:
		return account_key

	account_key = create_account_key()
	settings.letsencrypt_account_key = account_key
	settings.save()
	return account_key


def renew_certificates() -> None:
	"""Daily: put each certificate on every machine of its Active service, re-issued first when due. A
	machine missed one day gets it the next."""
	for name in frappe.get_all("Service Certificate", pluck="name"):
		try:
			proxy = get_serving_proxy(name)
			if proxy:
				proxy.renew_certificate()
		except Exception:
			frappe.log_error(title=f"Unable to renew the {name} certificate")


def get_serving_proxy(name: str) -> Nginx | None:
	"""The nginx that serves certificate `name`, or None while its service is not Active."""
	if name == "object-storage":
		cluster = frappe.db.get_value("Object Storage Cluster", {"status": "Active"})
		return frappe.get_doc("Object Storage Cluster", cluster).proxy if cluster else None

	if name == "telemetry":
		server = frappe.get_single("Datum Server")
		return server.proxy if server.status == "Active" else None

	frappe.throw(_("No service serves the {0} certificate.").format(name))
