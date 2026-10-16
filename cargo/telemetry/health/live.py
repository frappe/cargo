from __future__ import annotations

import typing

import frappe
import requests

from cargo.atlas_client import host_port
from cargo.telemetry.doctype.datum_server.datum_server import (
	TELEMETRY_READ_SITE_NAME,
	TELEMETRY_WRITE_SITE_NAME,
)

if typing.TYPE_CHECKING:
	from cargo.telemetry.doctype.datum_server.datum_server import DatumServer

ROUTING_PORT = 80
INSIGHTS_USER = "insights"
READ_PROBE = "SELECT 1 FROM datum.samples LIMIT 1"
PING_TIMEOUT = 10


def ping_host_machine() -> None:
	"""Mark the datum host Unreachable when datum or ClickHouse stops answering, and Active once
	both do again. Scheduled in `hooks.py`. Not Failed: the spawner re-runs setup on Failed, and
	an outage is no reason to reinstall."""
	server: DatumServer = frappe.get_single("Datum Server")
	if server.status not in ("Active", "Unreachable") or not server.machine:
		return

	reason = get_unreachable_reason(server)
	if reason and (server.status, server.error) != ("Unreachable", reason):
		server.mark("Unreachable", reason)
	elif not reason and server.status == "Unreachable":
		server.mark("Active")


def get_unreachable_reason(server: DatumServer) -> str | None:
	"""Why datum or ClickHouse on the host does not answer, or None when both do. Goes through
	the host's own nginx, the route the proxy takes, and reads as Insights does."""
	address = frappe.db.get_value("Machine", server.machine, "address")
	try:
		request_host(server, address, TELEMETRY_WRITE_SITE_NAME, "GET", "/health")
	except requests.RequestException as error:
		return f"datum did not answer: {error}"

	try:
		request_host(
			server,
			address,
			TELEMETRY_READ_SITE_NAME,
			"POST",
			"/",
			headers={
				"X-ClickHouse-User": INSIGHTS_USER,
				"X-ClickHouse-Key": server.get_password("insights_user_password"),
			},
			data=READ_PROBE,
		)
	except requests.RequestException as error:
		return f"ClickHouse did not answer a read: {error}"

	return None


def request_host(
	server: DatumServer,
	address: str,
	site_name: str,
	method: str,
	path: str,
	headers: dict | None = None,
	**kwargs,
) -> requests.Response:
	"""One request to a subdomain the host's nginx routes. Raises unless it succeeds."""
	response = requests.request(
		method,
		f"http://{host_port(address, ROUTING_PORT)}{path}",
		headers={"Host": f"{site_name}.{server.wildcard_domain}", **(headers or {})},
		timeout=PING_TIMEOUT,
		**kwargs,
	)
	response.raise_for_status()

	return response
