import frappe
from frappe.utils.password import remove_encrypted_password, set_encrypted_password

SETTINGS = {
	"central_url": "http://central.test",
	"central_webhook_url": "http://central.test/api/method/central.api.state_delivery.receive",
	"central_webhook_enabled": 1,
	"atlas_url": "http://atlas.test",
	"cargo_url": "http://cargo.test",
	"jwks_url": "http://atlas.test/api/atlas/jwks.json",
	"proxy_url": "http://proxy.test",
	"wildcard_domain": "example.test",
	"region_id": 1,
	"region": "test-region",
	"atlas_tenant_id": 0,
	# Release tracking is off unless a test turns it on, whatever ran before it.
	"track_pilot_releases": 0,
	"max_auto_retry_count": 3,
}
SECRETS = {
	"atlas_token": "test-atlas-token",
	"central_webhook_secret": "test-webhook-secret",
	"proxy_token": "test-proxy-token",
}
DATUM_SECRETS = ("datum_user_password", "insights_user_password", "default_user_password")


def use_test_settings() -> None:
	"""Stand Cargo Settings up for one test: a cluster cannot be inserted without them.

	Written inside the test's transaction, so it is rolled back with everything else and no
	site is left holding a made-up token."""
	for field, value in SETTINGS.items():
		frappe.db.set_single_value("Cargo Settings", field, value)

	for field, secret in SECRETS.items():
		set_encrypted_password("Cargo Settings", "Cargo Settings", secret, field)

	frappe.clear_document_cache("Cargo Settings", "Cargo Settings")


def reset_datum_server() -> None:
	"""Clear the datum host between tests: a Single has no row to delete, and its secrets
	live apart."""
	frappe.db.delete("Singles", {"doctype": "Datum Server"})
	for field in DATUM_SECRETS:
		remove_encrypted_password("Datum Server", "Datum Server", field)

	frappe.clear_document_cache("Datum Server", "Datum Server")
