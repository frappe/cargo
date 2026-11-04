from __future__ import annotations

import typing
from collections.abc import Callable
from urllib.parse import urlsplit

import frappe
import requests
from frappe import _

from cargo.atlas_client import host_port
from cargo.ssh import SSH_TIMEOUT, run_over_ssh, script

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.machine.machine import Machine
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)

CADDY_CONF = ("object_storage", "conf", "caddy", "install.sh")
S3_SITE_NAME = "s3-svc"
S3_ADMIN_SITE_NAME = "s3-admin-svc"
PROXY_SITE_NAMES = (S3_SITE_NAME, S3_ADMIN_SITE_NAME)
PROBE_TIMEOUT_SECONDS = 10


class Proxy:
	"""Caddy on a cluster's proxy node: public TLS for its domains, routed to the gateway."""

	def __init__(self, cluster: ObjectStorageCluster) -> None:
		self.cluster = cluster

	@property
	def machine(self) -> Machine:
		proxy = self.cluster.proxy_node
		if not proxy or proxy.status != "Running":
			frappe.throw(_("This cluster has no running proxy node."))

		if not proxy.public_ipv4:
			frappe.throw(_("Proxy node {0} has no public IPv4 address.").format(proxy.name))

		return proxy

	def install(self, on_output: Callable[[str], None] | None = None) -> None:
		"""Install Caddy and write the Caddyfile. Safe to repeat."""
		machine = self.machine
		run_over_ssh(
			machine.address,
			script(*CADDY_CONF, environment=self.environment),
			machine.get_password("ssh_private_key"),
			timeout=SSH_TIMEOUT,
			on_output=on_output,
		)

	@property
	def environment(self) -> dict[str, str]:
		gateway = self.cluster.gateway_address
		return {
			"WILDCARD_DOMAIN": self.cluster.wildcard_domain,
			"S3_UPSTREAM": host_port(gateway, self.cluster.s3_port),
			"ADMIN_UPSTREAM": host_port(gateway, self.cluster.admin_port),
			"CORS_ORIGIN": self.central_origin,
		}

	@property
	def central_origin(self) -> str:
		"""The one origin S3 accepts browser calls from: the Central dashboard's."""
		url = urlsplit(frappe.db.get_single_value("Cargo Settings", "central_url") or "")
		if not (url.scheme and url.netloc):
			frappe.throw(_("Central URL must be set in Cargo Settings."))

		return f"{url.scheme}://{url.netloc}"

	@property
	def is_serving(self) -> bool:
		"""Whether both domains answer over public DNS with a valid certificate."""
		domain = self.cluster.wildcard_domain
		try:
			admin = requests.get(
				f"https://{S3_ADMIN_SITE_NAME}.{domain}/health", timeout=PROBE_TIMEOUT_SECONDS
			)
			s3 = requests.head(f"https://{S3_SITE_NAME}.{domain}/", timeout=PROBE_TIMEOUT_SECONDS)
		except requests.RequestException:
			return False

		# S3 has no health route and refuses an unsigned call; Caddy answers 502 for a dead gateway.
		return admin.ok and s3.status_code < 500
