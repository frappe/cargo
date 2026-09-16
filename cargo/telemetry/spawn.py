"""Bringing a region its datum host without an operator."""

from __future__ import annotations

import typing

import frappe
from frappe import _
from frappe.utils.file_lock import LockTimeoutError

from cargo.cargo.doctype.machine.machine import DEAD_MACHINE_STATES
from cargo.client_models import TELEMETRY
from cargo.spawn import (
	has_required_settings,
	machine_status,
	report,
	spawn_config,
	spawn_lock,
	validate_node_size,
)

if typing.TYPE_CHECKING:
	from cargo.telemetry.doctype.datum_server.datum_server import DatumServer

CONFIG_KEY = "default_telemetry_config"
LOCK_NAME = "telemetry-spawn"
DATUM_FIELDS = ("repository", "version")
# Setting up again rents no machine, so a transient fault is worth another run. Three is
# where saying so beats trying again.
MAX_SETUP_ATTEMPTS = 3


def ensure_telemetry() -> None:
	"""Give this region one datum host and keep it moving.

	Off until `default_telemetry_config` is in site config. Scheduled in `hooks.py`."""
	config = spawn_config(CONFIG_KEY, validate_config)
	if not config or not has_required_settings():
		return

	try:
		with spawn_lock(LOCK_NAME):
			build_server(config)
	except LockTimeoutError:
		# Another run holds it and is already doing this work. Nothing here is urgent enough
		# to wait for: the next run picks up wherever that one leaves the region.
		return


def validate_config(config: dict) -> None:
	"""A shape Cargo can build a datum host from. Throws, naming what is wrong.

	Everything a Datum Server needs that has no default of its own, plus the machine to run
	it on: a host cannot be inserted without the first, or rented without the second."""
	if not isinstance(config, dict):
		frappe.throw(_("{0} must be an object.").format(CONFIG_KEY))

	for field in DATUM_FIELDS:
		if not isinstance(config.get(field), str) or not config[field].strip():
			frappe.throw(_("{0} must be set.").format(field))

	validate_node_size(config.get(TELEMETRY), TELEMETRY)


def build_server(config: dict) -> None:
	"""One step towards the region having a datum host that serves."""
	name = frappe.db.exists("Datum Server", {"auto_spawn": 1})
	if not name:
		if not frappe.db.count("Datum Server"):
			create_server(config)

		return

	server: DatumServer = frappe.get_doc("Datum Server", name)

	# A setup run is already under way, and it owns the host until it ends.
	if server.status == "Setting Up":
		return

	if fill_machine(server, config):
		advance(server)


def create_server(config: dict) -> DatumServer:
	"""One host, on its defaults. The machine is asked for on the next run, so a failure here
	leaves a record to carry on from rather than a rented machine with no owner.

	Datum checks tokens against Central's keys, so the issuer is where this Cargo reaches
	Central -- a host with neither issuer nor key answers 401 to everything."""
	return frappe.get_doc(
		{
			"doctype": "Datum Server",
			"auto_spawn": 1,
			"oidc_issuer": frappe.db.get_single_value("Cargo Settings", "central_url"),
			**{field: config[field] for field in DATUM_FIELDS},
		}
	).insert(ignore_permissions=True)


def fill_machine(server: DatumServer, config: dict) -> bool:
	"""Ask Atlas for the machine this host runs on. True once it has one."""
	if server.machine:
		if machine_status(server.machine) in DEAD_MACHINE_STATES:
			# Replacing a machine unattended is how a spawner runs away with money, and one
			# that would not boot is worth a look.
			report(
				server,
				_("{0} did not come up. Release it, and Cargo asks Atlas for another.").format(
					server.machine
				),
			)
			return False

		return True

	size = config[TELEMETRY]
	try:
		server.create_telemetry_node(
			cpu_millicores=size["cpu_millicores"],
			ram_gb=size["ram_gb"],
			disk_gb=size["disk_gb"],
		)
	except Exception:
		frappe.log_error(title=f"{server.name} could not add its machine")
		return False

	return True


def advance(server: DatumServer) -> None:
	"""Set the host up once its machine is up, and try again if a run failed."""
	if machine_status(server.machine) != "Running":
		return

	if server.status == "Draft":
		server.setup()
		return

	if server.status != "Failed":
		return

	# Spent. The record says how many runs it took and why the last one failed, so there is
	# nothing to add: from here the host waits for a person.
	if server.auto_setup_attempts >= MAX_SETUP_ATTEMPTS:
		return

	server.db_set("auto_setup_attempts", server.auto_setup_attempts + 1)
	server.setup()
