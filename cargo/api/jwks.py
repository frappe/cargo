import frappe
from werkzeug.wrappers import Response

from cargo.issuer import get_merged_jwks


# nosemgrep: guest-whitelisted-method -- a JWKS is public by design: it holds only public keys.
@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_jwks() -> Response:
	"""The key set that this region's Datum verifies tokens against."""
	response = Response(mimetype="application/json")
	response.data = frappe.as_json(get_merged_jwks())

	return response
