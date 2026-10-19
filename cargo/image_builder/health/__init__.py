import frappe

from cargo.image_builder.health.relay import Telemetry
from cargo.telemetry.credential import get_metrics_info

__all__ = ["Telemetry", "ship_metrics"]


def ship_metrics() -> None:
	"""Relay the image building metrics to datum. Scheduled hourly in `hooks.py`.

	A host whose bench has no datum credentials yet has nothing to ship, which is a state
	to wait out rather than report every hour."""
	try:
		info = get_metrics_info()
	except RuntimeError:
		return

	try:
		Telemetry(info["endpoint"], info["token"]).send()
	except Exception:
		frappe.log_error(title="Could not ship Pilot image metrics")
