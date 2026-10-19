import frappe
from frappe import _

from cargo.auth import verify_token
from cargo.object_storage.health import install_garage_metrics_exporters
from cargo.telemetry.credential import write_telemetry_credential
from cargo.telemetry.doctype.datum_server.datum_server import install_datum_metrics_exporter


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True, methods=["POST"])
@verify_token
def accept_datum_token(token: str) -> None:
	"""Accept a Datum token from Central; Cargo ships its own telemetry with it."""
	if not token:
		frappe.throw(_("A Datum token is required."), frappe.ValidationError)

	endpoint = frappe.get_single("Datum Server").service_endpoint
	write_telemetry_credential(endpoint, token)
	install_garage_metrics_exporters()
	install_datum_metrics_exporter()
