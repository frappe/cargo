from __future__ import annotations

import typing

import frappe
from frappe import _

from cargo.atlas_client import host_port
from cargo.caddy import Caddy, answers, require_public

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.machine.machine import Machine
	from cargo.telemetry.doctype.datum_server.datum_server import DatumServer

DATUM_PORT = 8000
TELEMETRY_WRITE_SITE_NAME = "telemetry-svc"
TELEMETRY_READ_SITE_NAME = "telemetry-read-svc"
LOOPBACK = "127.0.0.1"


class Proxy(Caddy):
	"""Caddy on the datum host: public TLS for its domains, routed to datum and ClickHouse on
	loopback, which is all either listens on."""

	caddyfile = ("telemetry", "conf", "caddy", "Caddyfile")

	def __init__(self, server: DatumServer) -> None:
		self.server = server

	@property
	def machine(self) -> Machine:
		machine = frappe.get_doc("Machine", self.server.machine) if self.server.machine else None
		return require_public(machine, _("datum host"))

	@property
	def domains(self) -> tuple[str, ...]:
		return self.server.proxy_domains

	@property
	def environment(self) -> dict[str, str]:
		return {
			"WILDCARD_DOMAIN": self.server.wildcard_domain,
			"DATUM_UPSTREAM": host_port(LOOPBACK, DATUM_PORT),
			"CLICKHOUSE_UPSTREAM": host_port(LOOPBACK, self.server.clickhouse_port),
		}

	@property
	def is_serving(self) -> bool:
		"""Whether datum and ClickHouse both answer their health checks over public TLS."""
		domain = self.server.wildcard_domain
		datum = answers(f"https://{TELEMETRY_WRITE_SITE_NAME}.{domain}/health")
		clickhouse = answers(f"https://{TELEMETRY_READ_SITE_NAME}.{domain}/ping")

		return bool(datum and datum.ok and clickhouse and clickhouse.ok)
