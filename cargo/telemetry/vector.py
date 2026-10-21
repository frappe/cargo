from __future__ import annotations

from cargo.ssh import script
from cargo.telemetry.credential import get_metrics_info

VECTOR_VERSION = "0.43.1"
SCRIPT = ("telemetry", "conf", "vector", "install.sh")


def install_script(
	role: str,
	machine: str,
	region: str,
	cluster: str | None = None,
	environment: dict[str, str | int] | None = None,
) -> str:
	"""The script that installs Vector on a rented machine, or hands it a new Datum token.
	`environment` carries what only this role needs. Raises `RuntimeError` while this host
	has no Datum token to hand over."""
	info = get_metrics_info()
	variables = {
		"VECTOR_VERSION": VECTOR_VERSION,
		"DATUM_ENDPOINT": info["endpoint"],
		"DATUM_TOKEN": info["token"],
		"REGION": region,
		"ROLE": role,
		"MACHINE": machine,
		**(environment or {}),
	}
	if cluster:
		variables["CLUSTER"] = cluster

	return script(*SCRIPT, environment=variables)
