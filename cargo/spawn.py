"""What every spawner needs: whether this host may build anything, and what to build."""

from __future__ import annotations

import typing

import frappe
from frappe import _
from frappe.utils.synchronization import filelock

from cargo.atlas_client import MAXIMUM_CPU_MILLICORES, MINIMUM_CPU_MILLICORES

if typing.TYPE_CHECKING:
	from collections.abc import Callable
	from contextlib import AbstractContextManager

	from frappe.model.document import Document

# Without these nothing can be built, set up, or reported to Central. The same for every
# service: they all rent from Atlas, answer under the region's domain, and report in.
REQUIRED_SETTINGS = ("region", "wildcard_domain", "atlas_url", "central_url", "proxy_url")
REQUIRED_SECRETS = ("atlas_token", "central_webhook_secret", "proxy_token")


def has_required_settings() -> bool:
	"""A half provisioned host is quiet rather than noisy: it is still being installed.

	A Password field reads back empty from the document, so the secrets are asked for by
	name rather than counted with the rest."""
	settings = frappe.get_cached_doc("Cargo Settings")
	if not all(settings.get(field) for field in REQUIRED_SETTINGS):
		return False

	return all(settings.get_password(field, raise_exception=False) for field in REQUIRED_SECRETS)


def spawn_config(config_key: str, validate: Callable[[dict], None]) -> dict | None:
	"""What to build, from site config. Absent means this region builds none, and a config
	that cannot be used is logged once and treated the same way."""
	config = frappe.conf.get(config_key)
	if not config:
		return None

	try:
		validate(config)
	except frappe.ValidationError as error:
		frappe.log_error(title=f"{config_key} is not usable", message=str(error))
		return None

	return config


def spawn_lock(name: str) -> AbstractContextManager:
	"""One run of a spawner at a time for this site."""
	return filelock(name, timeout=0)


def report(doc: Document, reason: str) -> None:
	"""Say why a spawn is stuck, once rather than on every run."""
	if doc.error != reason:
		doc.db_set("error", reason)


def machine_status(name: str) -> str:
	return frappe.db.get_value("Machine", name, "status")


def validate_node_size(size: object, role: str) -> None:
	"""A machine shape Atlas will accept. Throws, naming what is wrong."""
	if not isinstance(size, dict):
		frappe.throw(_("{0} must hold cpu_millicores, ram_gb and disk_gb.").format(role))

	for field in ("cpu_millicores", "ram_gb", "disk_gb"):
		if not isinstance(size.get(field), int) or isinstance(size[field], bool) or size[field] < 1:
			frappe.throw(_("{0}.{1} must be a whole number of at least 1.").format(role, field))

	if not MINIMUM_CPU_MILLICORES <= size["cpu_millicores"] <= MAXIMUM_CPU_MILLICORES:
		frappe.throw(
			_("{0}.cpu_millicores must be between {1} and {2}.").format(
				role, MINIMUM_CPU_MILLICORES, MAXIMUM_CPU_MILLICORES
			)
		)
