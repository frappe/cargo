import frappe
from frappe.utils.password import remove_encrypted_password, set_encrypted_password

SETTINGS = {
	"central_url": "http://central.test",
	"central_webhook_url": "http://central.test/api/method/central.api.state_delivery.receive",
	"central_webhook_enabled": 1,
	"atlas_url": "http://atlas.test",
	"cargo_url": "http://cargo.test",
	"jwks_url": "http://atlas.test/api/atlas/jwks.json",
	"wildcard_domain": "example.test",
	"region_id": 1,
	"region": "test-region",
	"atlas_tenant_id": 0,
	# Release tracking is off unless a test turns it on, whatever ran before it.
	"track_pilot_releases": 0,
	"max_auto_retry_count": 3,
	"update_pilot_in_base_image": 0,
	"storage_node_spread": "Separate hosts",
	"storage_node_spread_within": "",
}
SECRETS = {
	"atlas_token": "test-atlas-token",
	"central_webhook_secret": "test-webhook-secret",
}
FRAPPE_BRANCHES = ("version-16", "develop")
SIGNUP_APPS = ("erpnext", "hrms", "helpdesk", "gameplan", "raven", "builder")
DATUM_SECRETS = ("datum_user_password", "insights_user_password", "default_user_password")


def use_test_settings() -> None:
	"""Stand Cargo Settings up for one test: a cluster cannot be inserted without them.

	Written inside the test's transaction, so it is rolled back with everything else and no
	site is left holding a made-up token."""
	for field, value in SETTINGS.items():
		frappe.db.set_single_value("Cargo Settings", field, value)

	for field, secret in SECRETS.items():
		set_encrypted_password("Cargo Settings", "Cargo Settings", secret, field)

	set_frappe_branches(FRAPPE_BRANCHES)
	set_signup_apps(SIGNUP_APPS)
	set_compliance_apps({})
	frappe.clear_document_cache("Cargo Settings", "Cargo Settings")


def set_frappe_branches(branches: tuple[str, ...]) -> None:
	set_settings_rows("frappe_branches", "Frappe Branch", "branch", branches)


def set_signup_apps(apps: tuple[str, ...]) -> None:
	set_settings_rows("signup_apps", "Signup App", "app", apps)


def set_compliance_apps(compliance_apps: dict[str, str]) -> None:
	"""Replace the compliance apps of each signup app, given as a comma separated list."""
	frappe.db.delete("Compliance App", {"parenttype": "Cargo Settings", "parentfield": "compliance_apps"})
	for index, (signup_app, apps) in enumerate(compliance_apps.items(), start=1):
		frappe.get_doc(
			{
				"doctype": "Compliance App",
				"parent": "Cargo Settings",
				"parenttype": "Cargo Settings",
				"parentfield": "compliance_apps",
				"idx": index,
				"signup_app": signup_app,
				"compliance_apps": apps,
			}
		).db_insert()


def set_settings_rows(parentfield: str, doctype: str, column: str, values: tuple[str, ...]) -> None:
	"""Replace the rows of a Cargo Settings table, without saving the whole Single."""
	frappe.db.delete(doctype, {"parenttype": "Cargo Settings", "parentfield": parentfield})
	for index, value in enumerate(values, start=1):
		frappe.get_doc(
			{
				"doctype": doctype,
				"parent": "Cargo Settings",
				"parenttype": "Cargo Settings",
				"parentfield": parentfield,
				"idx": index,
				column: value,
			}
		).db_insert()


def reset_datum_server() -> None:
	"""Clear the datum host between tests: a Single has no row to delete, and its secrets
	live apart."""
	frappe.db.delete("Singles", {"doctype": "Datum Server"})
	for field in DATUM_SECRETS:
		remove_encrypted_password("Datum Server", "Datum Server", field)

	frappe.clear_document_cache("Datum Server", "Datum Server")
