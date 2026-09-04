from __future__ import annotations

import typing
from typing import ClassVar

import frappe
from frappe import _
from frappe.utils import now_datetime

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)

#: A state every other state may reach.
ANY = "*"


class ObjectStorageLifecycle:
	"""The states a cluster moves through, and the only place they are written."""

	TRANSITIONS: ClassVar[dict[str, tuple[str, ...] | str]] = {
		"Pending": ("Draft", "Pending", "Failed"),
		"Machines Ready": ("Pending",),
		"Minting Failed": ("Machines Ready", "Minting Failed"),
		"Credentials Minted": ("Machines Ready", "Minting Failed"),
		"Setting Up": ("Credentials Minted", "Failed", "Active"),
		"Active": ("Setting Up", "Active"),
		"Failed": ANY,
	}
	#: A machine can be added while a cluster is being built, and while it is serving.
	ACCEPTS_MACHINES = ("Draft", "Pending", "Failed", "Active")

	def __init__(self, cluster: ObjectStorageCluster) -> None:
		self.cluster = cluster

	@property
	def status(self) -> str:
		return self.cluster.status

	@property
	def is_live(self) -> bool:
		"""A cluster that has served once."""
		return bool(self.cluster.activated_on)

	def can(self, status: str) -> bool:
		sources = self.TRANSITIONS.get(status)

		return sources == ANY or self.status in (sources or ())

	def to(self, status: str, error: str | None = None) -> None:
		if not self.can(status):
			frappe.throw(_(f"A {self.status} cluster cannot become {status}."))

		if status == "Active" and not self.cluster.activated_on:
			self.cluster.activated_on = now_datetime()

		self.cluster.status = status
		self.cluster.error = error
		self.cluster.save(ignore_permissions=True)

	def fail(self, reason: str) -> None:
		"""A cluster that has served goes back to serving: the loss belongs to its health."""
		if not self.is_live:
			self.to("Failed", reason)
			return

		frappe.log_error(title=f"{self.cluster.name} kept serving through a failure", message=reason)
		self.to("Active", error=reason)

	def machine_added(self) -> None:
		"""A live cluster keeps serving while its new machine boots; a build waits for it."""
		if self.is_live:
			self.cluster.save(ignore_permissions=True)
			return

		self.to("Pending")

	def require_machines_accepted(self) -> None:
		if self.status not in self.ACCEPTS_MACHINES:
			frappe.throw(_(f"This cluster is {self.status}, so it cannot take a machine right now."))

	def check_contract(self) -> None:
		"""Every state named here has to exist on the doctype."""
		field = self.cluster.meta.get_field("status")
		options = (field.options or "").split("\n") if field else []
		named = set(self.TRANSITIONS)
		named.update(source for sources in self.TRANSITIONS.values() if sources != ANY for source in sources)

		missing = sorted(status for status in named if status not in options)
		if missing:
			frappe.throw(_("This cluster has no status option {0}.").format(", ".join(missing)))
