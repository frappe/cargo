import os
import typing

import frappe
from frappe import _

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings


ENROLMENT_VARS = (
	"CENTRAL_URL",
	"ATLAS_URL",
	"CARGO_URL",
	"REGION_ID",
	"REGION",
	"ATLAS_TOKEN",
	"ATLAS_TENANT_ID",
	"PROXY_URL",
	"PROXY_TOKEN",
	"WILDCARD_DOMAIN",
	"CENTRAL_WEBHOOK_SECRET",
	"JWKS_URL",
)


def after_install() -> None:
	"""Take the upstreams setup.sh passed in. CI installs the app with none, so it skips
	this."""
	if os.getenv("CI"):
		return

	missing = [name for name in ENROLMENT_VARS if not os.getenv(name)]
	if missing:
		frappe.throw(_("Set {0} before installing Cargo.").format(", ".join(missing)))

	record_upstreams()
	complete_setup_wizard()


def complete_setup_wizard() -> None:
	"""Frappe holds the desk at the setup wizard until someone walks it, and a Cargo host has
	nobody to. Nothing is answered: the site serves one app and took its configuration from
	the environment above."""
	if frappe.is_setup_complete():
		return

	from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

	setup_complete({})


def record_upstreams() -> None:
	"""Record the service URLs and credentials that the provisioner supplies."""
	settings: CargoSettings = frappe.get_single("Cargo Settings")
	settings.central_url = os.getenv("CENTRAL_URL")
	settings.atlas_url = os.getenv("ATLAS_URL")
	settings.cargo_url = os.getenv("CARGO_URL")
	settings.region_id = os.getenv("REGION_ID")
	settings.region = os.getenv("REGION")
	settings.atlas_token = os.getenv("ATLAS_TOKEN")
	settings.atlas_tenant_id = os.getenv("ATLAS_TENANT_ID")
	settings.proxy_url = os.getenv("PROXY_URL")
	settings.proxy_token = os.getenv("PROXY_TOKEN")
	settings.wildcard_domain = os.getenv("WILDCARD_DOMAIN")
	settings.central_webhook_secret = os.getenv("CENTRAL_WEBHOOK_SECRET")
	settings.jwks_url = os.getenv("JWKS_URL")
	settings.save(ignore_permissions=True)
