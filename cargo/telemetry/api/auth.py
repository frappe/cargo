import frappe
from frappe import _

from cargo.auth import verify_token
from cargo.telemetry.credential import write_telemetry_credential


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True, methods=["POST"])
@verify_token
def accept_datum_token(token: str) -> None:
	"""Accept a Datum token from Central; Cargo ships its own telemetry with it."""
	if not token:
		frappe.throw(_("A Datum token is required."), frappe.ValidationError)

	write_telemetry_credential(frappe.get_single("Datum Server").service_endpoint, token)
