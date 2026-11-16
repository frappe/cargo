"""Which websites may call a bucket from a browser. Garage enforces this per bucket; Caddy
passes the browser's preflight through to it."""

from __future__ import annotations

import re
import typing
from urllib.parse import urlsplit

import frappe
from frappe import _

if typing.TYPE_CHECKING:
	from cargo.object_storage.client import Client

#: Older Garage accepts `corsRules` on UpdateBucket and ignores them without an error.
MINIMUM_GARAGE_VERSION = (2, 3, 0)
METHODS = ["GET", "HEAD", "PUT", "POST"]
DEFAULT_PORTS = {"http": 80, "https": 443}
HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")
VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def normalize_origin(value: str) -> str:
	"""`value` as a browser writes it in the Origin header, or a ValidationError."""
	url = urlsplit(value.strip())
	try:
		port = url.port
	except ValueError:
		port = -1

	host = url.hostname or ""
	if (
		url.scheme not in DEFAULT_PORTS
		or port == -1
		or not HOST.fullmatch(host)
		or url.username is not None
		or url.path not in ("", "/")
		or url.query
		or url.fragment
	):
		frappe.throw(
			_("{0} is not an origin. Write it as scheme://host or scheme://host:port, such as {1}.").format(
				frappe.bold(value), "https://files.example.com"
			),
			title=_("Invalid origin"),
		)

	if port in (None, DEFAULT_PORTS[url.scheme]):
		return f"{url.scheme}://{host}"

	return f"{url.scheme}://{host}:{port}"


def normalize_origins(values: list[str]) -> list[str]:
	"""Each origin once, in the order given."""
	return list(dict.fromkeys(normalize_origin(value) for value in values))


def dashboard_origin() -> str:
	"""The Central dashboard's origin. Every bucket allows it, for the dashboard's own uploads."""
	url = urlsplit(frappe.db.get_single_value("Cargo Settings", "central_url") or "")
	if not (url.scheme and url.netloc):
		frappe.throw(_("Central URL must be set in Cargo Settings."))

	return f"{url.scheme}://{url.netloc}"


def rules_for(origins: list[str]) -> list[dict[str, typing.Any]]:
	"""One rule per origin: Garage answers a match with all of its rule's origins joined by
	commas, and a browser refuses an Access-Control-Allow-Origin that names more than one."""
	return [
		{
			"AllowedOrigin": [origin],
			"AllowedMethod": METHODS,
			"AllowedHeader": ["*"],
			"ExposeHeader": ["ETag"],
		}
		for origin in dict.fromkeys([dashboard_origin(), *origins])
	]


def outdated_versions(garage: Client) -> list[str]:
	"""The Garage versions on this cluster's nodes that ignore bucket CORS rules."""
	versions = {node.get("garageVersion") for node in garage.status().get("nodes") or []}
	outdated = []
	for version in versions - {None}:
		match = VERSION.match(version)
		if not match or tuple(int(part) for part in match.groups()) < MINIMUM_GARAGE_VERSION:
			outdated.append(version)

	return sorted(outdated)


def supported_by(garage: Client) -> bool:
	return not outdated_versions(garage)


def require_support(garage: Client) -> None:
	outdated = outdated_versions(garage)
	if outdated:
		frappe.throw(
			_(
				"This cluster runs Garage {0}. Browser access to buckets needs Garage v{1} or later"
				" on every node. Upgrade Garage first."
			).format(", ".join(outdated), ".".join(str(part) for part in MINIMUM_GARAGE_VERSION)),
			title=_("Garage too old"),
		)
