from __future__ import annotations

import typing

import frappe

from cargo.object_storage.health.live import LiveHealth, prune_history
from cargo.object_storage.health.telemetry import Telemetry, get_metrics_info

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)

__all__ = ["LiveHealth", "Telemetry", "prune_history", "refresh_health", "ship_metrics"]


def live_clusters() -> list[str]:
	return frappe.get_all("Object Storage Cluster", filters={"activated_on": ("is", "set")}, pluck="name")


def refresh_health() -> None:
	"""Re-read every live cluster's health from its gateway. Scheduled in `hooks.py`."""
	for name in live_clusters():
		cluster: ObjectStorageCluster = frappe.get_doc("Object Storage Cluster", name)
		try:
			LiveHealth(cluster).record()
		except Exception:
			frappe.log_error(title=f"Could not read {name} health")


def ship_metrics() -> None:
	"""Relay every live cluster's Garage metrics to datum. Scheduled in `hooks.py`.

	A host whose bench has no datum credentials yet has nothing to ship, which is a state
	to wait out rather than report every five minutes."""
	try:
		get_metrics_info()
	except RuntimeError:
		return

	for name in live_clusters():
		cluster: ObjectStorageCluster = frappe.get_doc("Object Storage Cluster", name)
		try:
			Telemetry(cluster).ship()
		except Exception:
			frappe.log_error(title=f"Could not ship {name} metrics")
