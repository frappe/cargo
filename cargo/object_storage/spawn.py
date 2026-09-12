"""Bringing a region its object storage cluster without an operator."""

from __future__ import annotations

import typing

import frappe
from frappe import _

from cargo.cargo.doctype.machine.machine import DEAD_MACHINE_STATES
from cargo.client_models import GATEWAY, STORAGE, Role

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)

CONFIG_KEY = "default_storage_cluster_config"
# Setting up again rents no machine, so a transient fault is worth another run. A broken
# gateway is not, and three runs is where saying so beats trying again.
MAX_SETUP_ATTEMPTS = 3
# Without these a cluster cannot be inserted, set up, or reported to Central.
REQUIRED_SETTINGS = ("region", "wildcard_domain", "atlas_url", "central_url", "proxy_url")
REQUIRED_SECRETS = ("atlas_token", "central_webhook_secret", "proxy_token")


def ensure_cluster() -> None:
	"""Give this region one object storage cluster and keep it moving.

	Off until `default_storage_cluster_config` is in site config. Scheduled in `hooks.py`."""
	config = spawn_config()
	if not config or not has_required_settings():
		return

	cluster = auto_cluster()
	if not cluster:
		if not frappe.db.count("Object Storage Cluster"):
			create_cluster()

		return

	# A setup run is already under way, and it owns the cluster until it ends.
	if cluster.status == "Setting Up":
		return

	if fill_machines(cluster, config):
		advance(cluster)


def spawn_config() -> dict | None:
	"""The cluster to build, from site config. Absent means this region builds none."""
	config = frappe.conf.get(CONFIG_KEY)
	if not config:
		return None

	try:
		validate_config(config)
	except frappe.ValidationError as error:
		frappe.log_error(title=f"{CONFIG_KEY} is not usable", message=str(error))
		return None

	return config


def validate_config(config: dict) -> None:
	"""A shape Cargo can ask Atlas for. Throws, naming what is wrong."""
	if not isinstance(config, dict):
		frappe.throw(_("{0} must be an object.").format(CONFIG_KEY))

	if not isinstance(config.get("storage_node_count"), int) or config["storage_node_count"] < 1:
		frappe.throw(_("storage_node_count must be a whole number of at least 1."))

	for role in (GATEWAY, STORAGE):
		size = config.get(role)
		if not isinstance(size, dict):
			frappe.throw(_("{0} must hold cpu, ram_gb and disk_gb.").format(role))

		for field in ("cpu", "ram_gb", "disk_gb"):
			if not isinstance(size.get(field), int) or size[field] < 1:
				frappe.throw(_("{0}.{1} must be a whole number of at least 1.").format(role, field))


def has_required_settings() -> bool:
	"""A half provisioned host is quiet rather than noisy: it is still being installed.

	A Password field reads back empty from the document, so the secrets are asked for by
	name rather than counted with the rest."""
	settings = frappe.get_cached_doc("Cargo Settings")
	if not all(settings.get(field) for field in REQUIRED_SETTINGS):
		return False

	return all(settings.get_password(field, raise_exception=False) for field in REQUIRED_SECRETS)


def auto_cluster() -> ObjectStorageCluster | None:
	"""The cluster Cargo made, if it made one."""
	name = frappe.db.exists("Object Storage Cluster", {"auto_spawn": 1})

	return frappe.get_doc("Object Storage Cluster", name) if name else None


def create_cluster() -> ObjectStorageCluster:
	"""One cluster, on its defaults. Machines are asked for on the next run, so a failure
	here leaves a record to carry on from rather than a rented machine with no owner."""
	return frappe.get_doc({"doctype": "Object Storage Cluster", "auto_spawn": 1}).insert()


def missing_slots(cluster: ObjectStorageCluster, config: dict) -> list[Role]:
	"""What this cluster still needs, gateway first: every other node joins through it."""
	alive = [row.role for row in cluster.machines if machine_status(row.machine) not in DEAD_MACHINE_STATES]
	gateways = 1 - alive.count(GATEWAY)
	storage = config["storage_node_count"] - alive.count(STORAGE)

	return [GATEWAY] * max(gateways, 0) + [STORAGE] * max(storage, 0)


def fill_machines(cluster: ObjectStorageCluster, config: dict) -> bool:
	"""Ask Atlas for the machines this cluster is short of. True once it has them all."""
	dead = [row.machine for row in cluster.machines if machine_status(row.machine) in DEAD_MACHINE_STATES]
	if dead:
		# Replacing a machine unattended is how a spawner runs away with money, and one that
		# would not boot is worth a look. Release it and the next run asks Atlas for another.
		report(
			cluster,
			_("{0} did not come up. Release it, and Cargo asks Atlas for another.").format(
				", ".join(sorted(dead))
			),
		)
		return False

	for role in missing_slots(cluster, config):
		size = config[role]
		try:
			cluster.add_node(role, cpu=size["cpu"], ram_gb=size["ram_gb"], disk_gb=size["disk_gb"])
		except Exception:
			frappe.log_error(title=f"{cluster.name} could not add a {role} machine")
			return False

		# Each machine in a transaction of its own: `Machine.request` rolls back the row it
		# failed on, and an uncommitted sibling would take a machine Atlas already built.
		if not frappe.flags.in_test:
			frappe.db.commit()  # nosemgrep

	return not missing_slots(cluster, config)


def advance(cluster: ObjectStorageCluster) -> None:
	"""Set the cluster up once its machines are up, and try again if a run failed."""
	if cluster.status == "Draft":
		if all(machine.status == "Running" for machine in cluster.all_nodes):
			cluster.setup()

		return

	# Only bring-up retries. A live cluster fails because its machines died, and setting up
	# again cannot raise the dead.
	if cluster.status != "Failed" or cluster.is_live:
		return

	# Spent. The record says how many runs it took and why the last one failed, so there is
	# nothing to add: from here the cluster waits for a person.
	if cluster.auto_setup_attempts >= MAX_SETUP_ATTEMPTS:
		return

	cluster.db_set("auto_setup_attempts", cluster.auto_setup_attempts + 1)
	cluster.setup()


def report(cluster: ObjectStorageCluster, reason: str) -> None:
	"""Say why the cluster is stuck, once rather than on every run."""
	if cluster.error != reason:
		cluster.db_set("error", reason)


def machine_status(name: str) -> str:
	return frappe.db.get_value("Machine", name, "status")
