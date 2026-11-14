from __future__ import annotations

from datetime import UTC, datetime

import frappe
import requests
from frappe.utils import get_datetime, now_datetime

from cargo.telemetry.datum import send_to_datum

METADATA_BACKUP_AGE = "cargo_object_storage_metadata_backup_age_seconds"
HEALTH = "cargo_object_storage_health"


def get_samples() -> list[dict]:
	"""The age of the newest uploaded metadata backup of each live cluster. A cluster that never uploaded
	one counts from its activation, so a backup that never starts also grows old."""
	now = now_datetime()
	timestamp = datetime.now(UTC).isoformat()
	clusters = frappe.get_all(
		"Object Storage Cluster",
		filters={"activated_on": ("is", "set")},
		fields=["name", "activated_on", "last_metadata_snapshot_at"],
	)
	return [
		{
			"metric": METADATA_BACKUP_AGE,
			"value": (
				now - get_datetime(cluster.last_metadata_snapshot_at or cluster.activated_on)
			).total_seconds(),
			"ts": timestamp,
			"labels": {"cluster": cluster.name},
		}
		for cluster in clusters
	]


def ship_metrics() -> None:
	"""Send the object storage metrics to Datum. Scheduled hourly in `hooks.py`."""
	samples = get_samples()
	if not samples:
		return

	try:
		send_to_datum("/v1/ingest", {"samples": samples})
	except RuntimeError:
		return
	except requests.RequestException:
		frappe.log_error(title="Could not ship object storage metrics")


def ship_health(health: dict[str, str]) -> None:
	"""Send the health of each live cluster, labelled with its verdict, each minute. A failed push is logged
	as a warning, not as an Error Log, so an unreachable Datum does not raise a Cargo error every minute."""
	if not health:
		return

	timestamp = datetime.now(UTC).isoformat()
	samples = [
		{"metric": HEALTH, "value": 1, "ts": timestamp, "labels": {"cluster": cluster, "health": verdict}}
		for cluster, verdict in health.items()
	]
	try:
		send_to_datum("/v1/ingest", {"samples": samples})
	except RuntimeError:
		return
	except requests.RequestException as error:
		frappe.logger("cargo.telemetry").warning(f"Datum object storage health push failed: {error}")
