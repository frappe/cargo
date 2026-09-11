import frappe
from frappe.utils.password import set_encrypted_password

SETTINGS = {
	"central_url": "http://central.test",
	"atlas_url": "http://atlas.test",
	"cargo_url": "http://cargo.test",
	"jwks_url": "http://atlas.test/api/atlas/jwks.json",
	"proxy_url": "http://proxy.test",
	"wildcard_domain": "example.test",
	"region_id": 1,
	"region": "test-region",
	"atlas_tenant_id": 0,
}
SECRETS = {
	"atlas_token": "test-atlas-token",
	"central_webhook_secret": "test-webhook-secret",
	"proxy_token": "test-proxy-token",
}


def use_test_settings() -> None:
	"""Stand Cargo Settings up for one test: a cluster cannot be inserted without them.

	Written inside the test's transaction, so it is rolled back with everything else and no
	site is left holding a made-up token."""
	for field, value in SETTINGS.items():
		frappe.db.set_single_value("Cargo Settings", field, value)

	for field, secret in SECRETS.items():
		set_encrypted_password("Cargo Settings", "Cargo Settings", secret, field)

	frappe.clear_document_cache("Cargo Settings", "Cargo Settings")
