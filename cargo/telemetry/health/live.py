from __future__ import annotations

import typing

import frappe
import requests

from cargo.telemetry.doctype.datum_server.proxy import TELEMETRY_READ_SITE_NAME, TELEMETRY_WRITE_SITE_NAME

if typing.TYPE_CHECKING:
	from cargo.telemetry.doctype.datum_server.datum_server import DatumServer

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
	the public domains and Caddy, the route pilots take, and reads as Insights does."""
	try:
		request_host(server, TELEMETRY_WRITE_SITE_NAME, "GET", "/health")
	except requests.RequestException as error:
		return f"datum did not answer: {error}"

	try:
		request_host(
			server,
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
	site_name: str,
	method: str,
	path: str,
	headers: dict | None = None,
	**kwargs,
) -> requests.Response:
	"""One request to a subdomain Caddy serves. Raises unless it succeeds."""
	response = requests.request(
		method,
		f"https://{site_name}.{server.wildcard_domain}{path}",
		headers=headers,
		timeout=PING_TIMEOUT,
		**kwargs,
	)
	response.raise_for_status()

	return response
