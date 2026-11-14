from __future__ import annotations

import typing
from dataclasses import dataclass
from pathlib import Path

import frappe
from datum_client.agent import get_install_script, get_role_config

from cargo.client_models import GATEWAY, STORAGE, TELEMETRY
from cargo.ssh import script, with_environment

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings

ROLE_FILES = Path(__file__).parent / "agent_roles"


@dataclass(frozen=True)
class AgentRole:
	"""What the Datum agent adds on a machine of one role: Cargo role files, shared role files of the agent,
	the units that the role files read, and the groups whose log files they read."""

	files: tuple[str, ...]
	shared: tuple[str, ...] = ()
	units: str = ""
	log_groups: str = ""


AGENT_ROLES = {
	GATEWAY: AgentRole(files=("garage",), shared=("nginx",), units="garage.service"),
	STORAGE: AgentRole(files=("garage",), units="garage.service"),
	TELEMETRY: AgentRole(files=("datum",), log_groups="clickhouse"),
}


def install_script(role: str, role_environment: dict[str, str | int] | None = None) -> str:
	"""The script that installs the Datum agent on a machine Cargo rented, after `datum_refresh_script`
	gave it a Datum token. `role_environment` carries what only the role files of this role read."""
	agent_role = AGENT_ROLES[role]
	documents = [(ROLE_FILES / f"{name}.yaml").read_text() for name in agent_role.files]
	documents += [get_role_config(name) for name in agent_role.shared]
	environment = {
		"PRODUCT": "cargo",
		"ROLE_UNITS": agent_role.units,
		"LOG_GROUPS": agent_role.log_groups,
		"ROLE_CONFIG": "\n---\n".join(documents),
		"ROLE_ENVIRONMENT": "\n".join(f"{key}={value}" for key, value in (role_environment or {}).items()),
	}

	return with_environment(get_install_script(), environment)


def datum_refresh_script() -> str:
	"""The script that has a machine fetch its own Datum token from Central every day, and ship
	to this region's Datum host."""
	settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")
	return script(
		"telemetry",
		"conf",
		"setup_datum_refresh.sh",
		environment={
			"CENTRAL_URL": settings.central_url,
			"DATUM_ENDPOINT": frappe.get_single("Datum Server").service_endpoint,
		},
	)
