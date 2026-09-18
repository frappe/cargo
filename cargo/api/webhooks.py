import frappe
from frappe import _
from frappe.utils import sbool
from frappe.utils.password import set_encrypted_password

from cargo.auth import verify_token

SETTINGS = "Cargo Settings"


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True, methods=["POST"])
@verify_token
def configure(request_url: str, webhook_secret: str, enabled: bool = True) -> dict:
	"""Point every Cargo delivery at one Central receiver.

	One receiver and one secret serve every service this region runs, so they live on
	Cargo Settings rather than on any one host or cluster. Each delivery reads them when it
	is built. A repeated call refreshes a rotated secret."""
	if not request_url or not webhook_secret:
		frappe.throw(_("A receiver needs a URL and a secret."), frappe.ValidationError)

	enabled = sbool(enabled)
	# Written field by field rather than through `save`: a Password reads back empty from
	# the document, so saving the whole Single fails its own mandatory checks.
	frappe.db.set_single_value(SETTINGS, "central_webhook_url", request_url)
	frappe.db.set_single_value(SETTINGS, "central_webhook_enabled", int(enabled))
	set_encrypted_password(SETTINGS, SETTINGS, webhook_secret, "central_webhook_secret")
	frappe.clear_document_cache(SETTINGS, SETTINGS)

	return {"request_url": request_url, "enabled": enabled}
