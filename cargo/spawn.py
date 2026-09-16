"""What every spawner needs: whether this host may build anything, and what to build."""

from __future__ import annotations

import typing

import frappe
from frappe.utils.synchronization import filelock

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
