from __future__ import annotations

import typing

import frappe
from frappe import _

from cargo.atlas_client import host_port
from cargo.nginx import Nginx, answers, require_public
from cargo.object_storage.doctype.object_storage_cluster.utils import (
	ensure_no_other_cluster_serving_or_setting_up,
)

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.machine.machine import Machine
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)

S3_SITE_NAME = "s3-svc"
S3_ADMIN_SITE_NAME = "s3-admin-svc"
PROXY_SITE_NAMES = (S3_SITE_NAME, S3_ADMIN_SITE_NAME)


class Proxy(Nginx):
	"""nginx on each gateway: public TLS for the cluster's domains, routed to that gateway's own
	Garage. DNS names every running gateway."""

	name = "object-storage"
	template = ("object_storage", "conf", "nginx", "object-storage.conf")

	def __init__(self, cluster: ObjectStorageCluster) -> None:
		self.cluster = cluster

	@property
	def machines(self) -> list[Machine]:
		gateways = self.cluster.running_gateways
		if not gateways:
			frappe.throw(_("The gateway is not running."))

		return [require_public(gateway, _("gateway")) for gateway in gateways]

	@property
	def health_check_url(self) -> str:
		return f"https://{S3_ADMIN_SITE_NAME}.{self.cluster.wildcard_domain}/health"

	@property
	def domains(self) -> tuple[str, ...]:
		return self.cluster.proxy_domains

	def publish_dns(self, machines: list[Machine], domains: tuple[str, ...]) -> None:
		ensure_no_other_cluster_serving_or_setting_up(self.cluster)
		super().publish_dns(machines, domains)

	def get_environment(self, machine: Machine) -> dict[str, str]:
		return {
			"WILDCARD_DOMAIN": self.cluster.wildcard_domain,
			"S3_UPSTREAM": host_port(machine.address, self.cluster.s3_port),
			"ADMIN_UPSTREAM": host_port(machine.address, self.cluster.admin_port),
		}

	@property
	def is_serving(self) -> bool:
		"""Whether both domains answer over public DNS with a valid certificate."""
		domain = self.cluster.wildcard_domain
		admin = answers(f"https://{S3_ADMIN_SITE_NAME}.{domain}/health")
		s3 = answers(f"https://{S3_SITE_NAME}.{domain}/", method="HEAD")

		# S3 has no health route and refuses an unsigned call; nginx answers 502 for a dead Garage.
		return bool(admin and admin.ok and s3 is not None and s3.status_code < 500)
