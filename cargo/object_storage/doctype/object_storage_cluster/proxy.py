from __future__ import annotations

import typing
from urllib.parse import urlsplit

import frappe
from frappe import _

from cargo.atlas_client import host_port
from cargo.caddy import Caddy, answers, require_public

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.machine.machine import Machine
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)

S3_SITE_NAME = "s3-svc"
S3_ADMIN_SITE_NAME = "s3-admin-svc"
PROXY_SITE_NAMES = (S3_SITE_NAME, S3_ADMIN_SITE_NAME)


class Proxy(Caddy):
	"""Caddy on a cluster's gateway: public TLS for its domains, routed to Garage."""

	caddyfile = ("object_storage", "conf", "caddy", "Caddyfile")

	def __init__(self, cluster: ObjectStorageCluster) -> None:
		self.cluster = cluster

	@property
	def machine(self) -> Machine:
		return require_public(self.cluster.gateway_node, _("gateway"))

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
		admin = answers(f"https://{S3_ADMIN_SITE_NAME}.{domain}/health")
		s3 = answers(f"https://{S3_SITE_NAME}.{domain}/", method="HEAD")

		# S3 has no health route and refuses an unsigned call; Caddy answers 502 for a dead gateway.
		return bool(admin and admin.ok and s3 is not None and s3.status_code < 500)
