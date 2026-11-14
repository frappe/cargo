import frappe

from cargo.image_builder.health.relay import Telemetry
from cargo.telemetry.credential import get_application_credential

__all__ = ["Telemetry", "ship_metrics"]


def ship_metrics() -> None:
	"""Relay the image building metrics to datum. Scheduled hourly in `hooks.py`.

	A region whose Datum host is not Active yet has nothing to ship to, which is a state
	to wait out rather than report every hour."""
	try:
		info = get_application_credential()
	except RuntimeError:
		return

	try:
		Telemetry(info["endpoint"], info["token"]).send()
	except Exception:
		frappe.log_error(title="Could not ship Pilot image metrics")
