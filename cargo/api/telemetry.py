import frappe

from cargo.issuer import DATUM_TOKEN_LIFETIME, issue_datum_token, verify_machine_token


# nosemgrep: guest-whitelisted-method -- verify_machine_token authenticates the caller below.
@frappe.whitelist(allow_guest=True, methods=["GET"])
def datum_token() -> dict:
	"""The Datum token that an object storage or Datum host machine sends as its Atlas Virtual Machine.
	Not the Authorization header: Frappe refuses a scheme it cannot map to a user."""
	virtual_machine = verify_machine_token(frappe.get_request_header("X-Cargo-Machine-Token") or "")
	return {"token": issue_datum_token(virtual_machine), "expires_in": DATUM_TOKEN_LIFETIME}
