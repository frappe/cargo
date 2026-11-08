from __future__ import annotations

import frappe
from frappe import _


def is_mesh_only_enabled() -> bool:
	"""Validate the opt-in development path before changing regional resources."""
	if not frappe.conf.get("dev_mesh_only_proxy"):
		return False
	if not frappe.conf.get("developer_mode") or not (
		frappe.db.get_single_value("Cargo Settings", "wildcard_domain") or ""
	).endswith(".test"):
		frappe.throw(_("Mesh-only Cargo requires developer mode and a .test wildcard domain."))
	return True


def is_telemetry_disabled() -> bool:
	if not frappe.conf.get("dev_cargo_disable_telemetry"):
		return False
	if not is_mesh_only_enabled():
		frappe.throw(_("Disabling telemetry requires mesh-only development mode."))
	return True
