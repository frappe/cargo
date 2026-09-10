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
	"ATLAS_KEY",
	"ATLAS_SECRET",
	"ATLAS_TENANT_ID",
	"CENTRAL_BOOTSTRAPPING_TOKEN",
)


def after_install() -> None:
	"""Enrol this host with Central, using the bootstrapping token setup.sh passed in.
	Skip CI in this case since the environment variables are not set there."""
	if not any(os.getenv(name) for name in ENROLMENT_VARS):
		frappe.throw(_("Set {0} before installing Cargo.").format(", ".join(ENROLMENT_VARS)))

	record_bootstrapping_token()


def record_bootstrapping_token() -> None:
	"""Take the upstream URLs and the one-time token out of the environment."""
	settings: CargoSettings = frappe.get_single("Cargo Settings")
	settings.central_url = os.getenv("CENTRAL_URL")
	settings.atlas_url = os.getenv("ATLAS_URL")
	settings.cargo_url = os.getenv("CARGO_URL")
	settings.region_id = os.getenv("REGION_ID")
	settings.region = os.getenv("REGION")
	settings.atlas_key = os.getenv("ATLAS_KEY")
	settings.atlas_secret = os.getenv("ATLAS_SECRET")
	settings.atlas_tenant_id = os.getenv("ATLAS_TENANT_ID")
	settings.central_webhook_secret = os.getenv("CENTRAL_BOOTSTRAPPING_TOKEN")
	settings.save(ignore_permissions=True)


TEST_SETTINGS = {
	"central_url": "http://central.test",
	"atlas_url": "http://atlas.test",
	"cargo_url": "http://cargo.test",
	"region_id": 1,
	"region": "test-region",
	"atlas_key": "test-key",
	"atlas_secret": "test-secret",
	"atlas_tenant_id": 1,
	"central_webhook_secret": "test-webhook-secret",
}


def before_tests() -> None:
	"""A test site is never enrolled, so stand its Cargo Settings up: a cluster cannot be
	inserted without them. Only what is missing, so a local site keeps what it points at."""
	settings: CargoSettings = frappe.get_single("Cargo Settings")
	settings.update({field: value for field, value in TEST_SETTINGS.items() if not settings.get(field)})
	settings.save(ignore_permissions=True)
	frappe.db.commit()
