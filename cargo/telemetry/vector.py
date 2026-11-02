from __future__ import annotations

import typing

import frappe

from cargo.ssh import script

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings

VECTOR_VERSION = "0.43.1"
SCRIPT = ("telemetry", "conf", "vector", "install.sh")


def install_script(
	role: str,
	machine: str,
	region: str,
	cluster: str | None = None,
	environment: dict[str, str | int] | None = None,
) -> str:
	"""The script that installs Vector on a rented machine, after `datum_refresh_script` gave it
	a Datum token. `environment` carries what only this role needs."""
	variables = {
		"VECTOR_VERSION": VECTOR_VERSION,
		"REGION": region,
		"ROLE": role,
		"MACHINE": machine,
		**(environment or {}),
	}
	if cluster:
		variables["CLUSTER"] = cluster

	return script(*SCRIPT, environment=variables)


def datum_refresh_script() -> str:
	"""The script that has a node fetch its own Datum token from Central every day, and ship
	to this region's Datum host."""
	settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")
	return script(
		"telemetry",
		"conf",
		"vector",
		"setup_datum_refresh.sh",
		environment={
			"CENTRAL_URL": settings.central_url,
			"DATUM_ENDPOINT": frappe.get_single("Datum Server").service_endpoint,
		},
	)
