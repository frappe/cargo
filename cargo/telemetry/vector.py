from __future__ import annotations

import os
from pathlib import Path

from cargo.ssh import script
from cargo.telemetry.credential import get_metrics_info

VECTOR_VERSION = "0.43.1"
SCRIPT = ("telemetry", "conf", "vector", "install.sh")
# Atlas's install-cargo.sh creates it empty and owned by the bench user, and restarts Vector
# whenever it changes. Absent on a host Atlas did not install, such as a laptop.
LOCAL_CREDENTIAL = Path("/etc/vector/datum.env")


def install_script(role: str, machine: str, region: str, cluster: str | None = None) -> str:
	"""The script that installs Vector on a rented machine, or hands it a new Datum token.
	Raises `RuntimeError` while this host has no Datum token to hand over."""
	info = get_metrics_info()
	environment = {
		"VECTOR_VERSION": VECTOR_VERSION,
		"DATUM_ENDPOINT": info["endpoint"],
		"DATUM_TOKEN": info["token"],
		"REGION": region,
		"ROLE": role,
		"MACHINE": machine,
	}
	if cluster:
		environment["CLUSTER"] = cluster

	return script(*SCRIPT, environment=environment)


def write_local_credential(endpoint: str, token: str, path: Path = LOCAL_CREDENTIAL) -> bool:
	"""Hand this Cargo host's own Vector the Datum token. False on a host without one."""
	if not path.exists():
		return False

	descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC)
	with os.fdopen(descriptor, "w") as credential:
		credential.write(f"DATUM_ENDPOINT={endpoint.rstrip('/')}\nDATUM_TOKEN={token}\n")

	return True
