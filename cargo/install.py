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
	"CENTRAL_WEBHOOK_SECRET",
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


def record_upstreams() -> None:
	"""Where this host reaches Central and Atlas, and the secret it signs Central's webhooks
	with. Every one of them comes from the provisioner, through the environment."""
	settings: CargoSettings = frappe.get_single("Cargo Settings")
	settings.central_url = os.getenv("CENTRAL_URL")
	settings.atlas_url = os.getenv("ATLAS_URL")
	settings.cargo_url = os.getenv("CARGO_URL")
	settings.region_id = os.getenv("REGION_ID")
	settings.region = os.getenv("REGION")
	settings.atlas_key = os.getenv("ATLAS_KEY")
	settings.atlas_secret = os.getenv("ATLAS_SECRET")
	settings.atlas_tenant_id = os.getenv("ATLAS_TENANT_ID")
	settings.central_webhook_secret = os.getenv("CENTRAL_WEBHOOK_SECRET")
	settings.save(ignore_permissions=True)
