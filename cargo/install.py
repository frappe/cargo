import os
import typing

import frappe
from frappe import _

from cargo.central_client import CentralClient

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings


ENROLMENT_VARS = ("CENTRAL_URL", "ATLAS_URL", "REGION", "CENTRAL_BOOTSTRAPPING_TOKEN")


def after_install() -> None:
	"""Enrol this host with Central, using the bootstrapping token setup.sh passed in.
	Skip CI in this case since the environment variables are not set there."""
	if not any(os.getenv(name) for name in ENROLMENT_VARS):
		return

	record_bootstrapping_token()
	request_control_credentials()


def record_bootstrapping_token() -> None:
	"""Take the upstream URLs and the one-time token out of the environment."""
	settings: CargoSettings = frappe.get_single("Cargo Settings")
	settings.central_url = os.getenv("CENTRAL_URL")
	settings.atlas_url = os.getenv("ATLAS_URL")
	settings.region = os.getenv("REGION")
	settings.central_bootstrapping_token = os.getenv("CENTRAL_BOOTSTRAPPING_TOKEN")

	if not all(os.getenv(name) for name in ENROLMENT_VARS):
		frappe.throw(_("Set {0} before installing Cargo.").format(", ".join(ENROLMENT_VARS)))

	settings.save(ignore_permissions=True)


def request_control_credentials() -> None:
	"""Trade the bootstrapping token for the two long-lived ones this host runs on."""
	settings: CargoSettings = frappe.get_single("Cargo Settings")
	token = settings.get_password("central_bootstrapping_token", raise_exception=False)
	if not (settings.central_url and token):
		frappe.throw(_("This host has no bootstrapping token to present to Central."))

	credentials = CentralClient(settings.central_url, token, is_bootstrapping=True).call(
		"request_control_credentials", data={"base_url": frappe.utils.get_url()}
	)

	settings.central_access_token = credentials.get("central_access_token")
	settings.atlas_access_token = credentials.get("atlas_access_token")
	if not (settings.central_access_token and settings.atlas_access_token):
		frappe.throw(_("Central did not return both access tokens."))

	settings.central_bootstrapping_token = None
	settings.save(ignore_permissions=True)
