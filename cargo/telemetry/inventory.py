from __future__ import annotations

from datetime import UTC, datetime

import frappe
import requests

from cargo.telemetry.credential import get_virtual_machine_name
from cargo.telemetry.datum import send_to_datum

MACHINE = "cargo_machine"
CARGO = "cargo"
VIRTUAL_MACHINE_KEY = "cargo:virtual_machine_name"


def get_machine_samples() -> list[dict]:
	"""One `cargo_machine` sample with the value 1 for each running machine of Cargo, and for the VM that runs
	Cargo with the role `cargo`. A machine sends no identity labels, so a reader finds its role and cluster here."""
	timestamp = datetime.now(UTC).isoformat()
	machines = frappe.get_all(
		"Machine",
		filters={"status": "Running", "vm_id": ("is", "set")},
		fields=["vm_id", "role", "reference_doctype", "reference_name"],
	)

	samples = [
		{"metric": MACHINE, "value": 1, "ts": timestamp, "labels": {"machine": get_cargo_vm(), "role": CARGO}}
	]
	for machine in machines:
		labels = {"machine": machine.vm_id, "role": machine.role}
		if machine.reference_doctype == "Object Storage Cluster":
			labels["cluster"] = machine.reference_name
		samples.append({"metric": MACHINE, "value": 1, "ts": timestamp, "labels": labels})

	return samples


def get_cargo_vm() -> str:
	"""The name of the VM that runs Cargo. It does not change, so the cache keeps it."""
	name = frappe.cache.get_value(VIRTUAL_MACHINE_KEY)
	if not name:
		name = get_virtual_machine_name()
		frappe.cache.set_value(VIRTUAL_MACHINE_KEY, name)
	return name


def ship_machines() -> None:
	"""Send the machine inventory to Datum each minute. A failed push is a warning, as in `ship_health`."""
	try:
		send_to_datum("/v1/ingest", {"samples": get_machine_samples()})
	except RuntimeError:
		return
	except requests.RequestException as error:
		frappe.logger("cargo.telemetry").warning(f"Datum machine inventory push failed: {error}")
