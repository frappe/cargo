from __future__ import annotations

import typing
from types import ModuleType
from typing import Any, Literal, Self

import frappe
import httpx
from atlas_client import AuthenticatedClient
from atlas_client.api.dns_records import remove_dns_record, set_dns_record
from atlas_client.api.images import (
	delete_image,
	get_image,
	list_images,
	update_image_termination_protection,
)
from atlas_client.api.vm_actions import (
	create_virtual_machine_snapshot,
	pause_virtual_machine,
	resize_virtual_machine,
	restart_virtual_machine,
	resume_virtual_machine,
	start_virtual_machine,
	stop_virtual_machine,
)
from atlas_client.api.vm_configuration import (
	attach_virtual_machine_public_ipv4,
	attach_virtual_machine_public_ipv6,
	detach_virtual_machine_public_ipv4,
	detach_virtual_machine_public_ipv6,
	replace_virtual_machine_metadata,
	update_virtual_machine_disk,
	update_virtual_machine_network,
	update_virtual_machine_termination_protection,
)
from atlas_client.api.vm_lifecycle import create_virtual_machine, delete_virtual_machine, get_virtual_machine
from atlas_client.models import (
	CreateVirtualMachinePayload,
	DiskUpdatePayload,
	DnsRecordPayload,
	DnsRecordPayloadTypeType0,
	ListImagesImageTypeType0,
	MetadataReplacementPayload,
	NetworkUpdatePayload,
	PublicIPAssignmentPayload,
	ResizePayload,
	SnapshotPayload,
	TerminationProtectionPayload,
)

from cargo.client_models import PlacementRule

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings

AUTO_PUBLIC_IP = "auto"
ANYWHERE = ["0.0.0.0/0", "::/0"]
IPVersion = Literal["IPv4", "IPv6"]
RUNNING_STATE = "running"
STOPPED_STATE = "stopped"
PAUSED_STATE = "paused"
DEAD_STATES = frozenset({"failed"})
MIB_PER_GB = 1024
# Atlas accepts this range; 1000 millicores is one core.
MINIMUM_CPU_MILLICORES = 100
MAXIMUM_CPU_MILLICORES = 32_000
# Atlas names an image by a generated id, so the one to boot on is found by its tags.
BASE_IMAGE_TAGS = {"purpose": "base", "os": "Ubuntu", "os_version": "26.04"}
# A Pilot image bakes on the base image, so it carries the same operating system.
PILOT_IMAGE_OS_TAGS = {key: BASE_IMAGE_TAGS[key] for key in ("os", "os_version")}
# The most a list route returns in one page.
IMAGE_PAGE_LIMIT = 100
POWER_ACTIONS = {
	"start": start_virtual_machine,
	"stop": stop_virtual_machine,
	"pause": pause_virtual_machine,
	"resume": resume_virtual_machine,
	"restart": restart_virtual_machine,
}
ATTACH_PUBLIC_IP = {"IPv4": attach_virtual_machine_public_ipv4, "IPv6": attach_virtual_machine_public_ipv6}
DETACH_PUBLIC_IP = {"IPv4": detach_virtual_machine_public_ipv4, "IPv6": detach_virtual_machine_public_ipv6}


class AtlasError(RuntimeError):
	"""An Atlas call failed. One argument, so it survives a pickle round trip: that is how
	the pipeline engine carries an exception back to the flow that raised it."""


class AtlasNotFound(AtlasError):
	"""Atlas has no such resource. A terminated machine reads as one."""


class AtlasClient:
	"""Atlas's tenant API, through the generated `atlas_client`. Every route is scoped to the tenant."""

	def __init__(self, url: str, token: str, tenant_id: int, timeout: float = 120) -> None:
		self.tenant_id = tenant_id
		self.client = AuthenticatedClient(
			base_url=url.rstrip("/"), token=token, timeout=httpx.Timeout(timeout)
		)

	@classmethod
	def from_settings(cls) -> Self:
		"""The Atlas client for the current site. `get_password`, not the attribute: a
		Password field reads back as its mask."""
		settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")

		return cls(
			url=settings.atlas_url,
			token=settings.get_password("atlas_token"),
			tenant_id=settings.atlas_tenant_id,
		)

	def call(self, endpoint: ModuleType, *args: Any, **kwargs: Any) -> Any:
		"""One request built by a generated endpoint. Returns the JSON body, or None when there is none.

		The body is read as JSON, not parsed by the endpoint: a strict parse that fails after Atlas
		created a machine would read as a failed create, and a retry would create a second one."""
		name = endpoint.__name__.rsplit(".", 1)[-1]
		request = endpoint._get_kwargs(*args, x_tenant_id=self.tenant_id, **kwargs)
		try:
			response = self.client.get_httpx_client().request(**request)
		except httpx.HTTPError as exception:
			raise AtlasError(f"{name}: {exception}") from exception

		try:
			payload = response.json() if response.content else None
		except ValueError:
			payload = None

		if response.status_code == 404:
			raise AtlasNotFound(f"{name}: {error_message(payload, response.text)}")
		if not response.is_success:
			raise AtlasError(
				f"{name} answered {response.status_code}: {error_message(payload, response.text)}"
			)

		return payload

	def set_dns_records(self, name: str, addresses: list[str], health_check_url: str | None = None) -> None:
		"""Point a regional service name at every address, replacing the ones it had. Atlas picks A
		or AAAA and a 300 second TTL. With `health_check_url`, Route53 asks that URL at each
		address and leaves out one that stops answering."""
		payload = DnsRecordPayload(addresses=addresses, health_check_url=health_check_url)
		self.call(set_dns_record, name, body=payload)

	def set_txt_record(self, name: str, values: list[str]) -> None:
		"""Replace the TXT values of a regional name, such as an ACME challenge. Values go unquoted."""
		payload = DnsRecordPayload(type_=DnsRecordPayloadTypeType0.TXT, values=values)
		self.call(set_dns_record, name, body=payload)

	def remove_txt_record(self, name: str) -> None:
		"""Remove a name Cargo set TXT values on. Atlas removes every record of the name."""
		self.call(remove_dns_record, name)

	def set_vm_firewall(self, vm_id: str, firewall: dict[str, Any]) -> None:
		"""Replace the VM's whole firewall. Atlas answers before Metal applies it."""
		self.call(
			update_virtual_machine_network, vm_id, body=NetworkUpdatePayload.from_dict({"firewall": firewall})
		)

	def replace_vm_metadata(self, machine_id: str, metadata: dict[str, str]) -> None:
		"""Replace the VM's whole metadata map. Atlas drops every key left out."""
		self.call(
			replace_virtual_machine_metadata,
			machine_id,
			body=MetadataReplacementPayload.from_dict({"metadata": metadata}),
		)

	def create_vm(
		self,
		*,
		image_id: str,
		cpu_millicores: int,
		memory_mib: int,
		disk_mib: int,
		public_key: str,
		hostname: str,
		metadata: dict[str, str] | None = None,
		tags: dict[str, str] | None = None,
		placement_rules: list[PlacementRule] | None = None,
		is_termination_protected: bool = False,
		has_public_ipv4: bool = False,
		is_privileged: bool = False,
	) -> dict[str, Any]:
		"""Ask Atlas for one machine and return the record it made."""
		payload = {
			"image_id": image_id,
			"cpu_millicores": cpu_millicores,
			"memory_mib": memory_mib,
			"disk_mib": disk_mib,
			"ssh_keys": [public_key],
			"hostname": hostname,
			"metadata": metadata or {},
			"ipv4_internet_access": True,
		}
		# Sent only when set: an Atlas without affinity rules rejects unknown fields.
		if tags:
			payload["tags"] = tags
		if placement_rules:
			payload["placement_rules"] = [rule.asdict() for rule in placement_rules]
		if is_termination_protected:
			payload["is_termination_protected"] = True
		if has_public_ipv4:
			payload["public_ipv4"] = AUTO_PUBLIC_IP
		# Lets tenant VMs reach the service over the mesh. Atlas allows it only for tenant 0.
		if is_privileged:
			payload["is_privileged"] = True

		created = self.call(create_virtual_machine, body=CreateVirtualMachinePayload.from_dict(payload))
		if not isinstance(created, dict) or not created.get("id"):
			raise AtlasError(f"create_virtual_machine returned no id: {created!r}")

		return created

	def get_vm(self, vm_id: str) -> dict[str, Any]:
		"""The VM as Atlas currently sees it, including `current_state`."""
		return self.call(get_virtual_machine, vm_id)

	def terminate_vm(self, vm_id: str) -> None:
		"""Start termination. The VM route answers 404 once cleanup finishes."""
		self.call(delete_virtual_machine, vm_id)

	def set_vm_termination_protection(self, vm_id: str, enabled: bool) -> None:
		"""Set or clear a VM's termination protection. Atlas refuses to terminate a protected VM."""
		self.call(
			update_virtual_machine_termination_protection,
			vm_id,
			body=TerminationProtectionPayload(enabled=enabled),
		)

	def attach_public_ip(self, vm_id: str, version: IPVersion, public_ip: str = AUTO_PUBLIC_IP) -> None:
		"""Ask Atlas for a public address: one it picks, or a reserved allocation by id. It
		answers before the address is live."""
		self.call(ATTACH_PUBLIC_IP[version], vm_id, body=PublicIPAssignmentPayload(public_ip=public_ip))

	def detach_public_ip(self, vm_id: str, version: IPVersion) -> None:
		"""Release the public address. A reserved allocation stays with the tenant."""
		self.call(DETACH_PUBLIC_IP[version], vm_id)

	def set_power_state(self, vm_id: str, action: str) -> None:
		"""Ask Atlas to start, stop, pause, resume or restart the VM. It answers before the VM gets there."""
		self.call(POWER_ACTIONS[action], vm_id)

	def grow_disk(self, vm_id: str, disk_mib: int) -> None:
		"""Grow the VM's disk while it runs."""
		self.call(update_virtual_machine_disk, vm_id, body=DiskUpdatePayload(disk_mib=disk_mib))

	def resize_vm(self, vm_id: str, *, cpu_millicores: int, memory_mib: int, disk_mib: int) -> None:
		"""Change the VM's shape. Atlas only resizes a stopped VM and only grows the disk."""
		self.call(
			resize_virtual_machine,
			vm_id,
			body=ResizePayload(cpu_millicores=cpu_millicores, memory_mib=memory_mib, disk_mib=disk_mib),
		)

	def create_snapshot(
		self,
		vm_id: str,
		title: str,
		*,
		image_type: Literal["machine", "system"] = "machine",
		cache_image: bool = False,
		memory_snapshot: bool = False,
		tags: dict[str, str] | None = None,
	) -> str:
		"""Freeze a machine's disk into an image Atlas can boot later."""
		body = SnapshotPayload.from_dict(
			{
				"title": title,
				"image_type": image_type,
				"cache_image": cache_image,
				"memory_snapshot": memory_snapshot,
				"tags": tags or {},
			}
		)
		created = self.call(create_virtual_machine_snapshot, vm_id, body=body)
		if not isinstance(created, dict) or not created.get("id"):
			raise AtlasError(f"create_snapshot returned no id: {created!r}")

		return created["id"]

	def set_image_termination_protection(self, image_id: str, enabled: bool) -> None:
		"""Set or clear an image's termination protection. Atlas protects every System image
		when it makes one, and refuses to delete a protected image."""
		self.call(
			update_image_termination_protection, image_id, body=TerminationProtectionPayload(enabled=enabled)
		)

	def delete_snapshot(self, image_id: str) -> None:
		"""Retire an image. Atlas archives one a machine still uses and reclaims it later."""
		self.call(delete_image, image_id)

	def find_system_image(self, tags: dict[str, str]) -> str | None:
		"""The id of the newest available System image carrying every tag, or None.

		Atlas matches the tags and returns enabled images newest first, so one page holds every candidate."""
		page = self.call(
			list_images,
			image_type=ListImagesImageTypeType0.SYSTEM,
			tag=",".join(f"{key}:{value}" for key, value in tags.items()),
			limit=IMAGE_PAGE_LIMIT,
		)
		for image in page.get("items") or []:
			if image.get("status") == "available":
				return image["id"]

		return None

	def get_snapshot(self, image_id: str) -> dict[str, Any]:
		"""The image as Atlas currently sees it, to know when it is usable."""
		return self.call(get_image, image_id)


def base_image_id(minimal: bool = False) -> str:
	"""The Ubuntu system image a Cargo machine boots on, minimal or full. Throws when Atlas
	has none."""
	tags = {**BASE_IMAGE_TAGS, "minimal": "true" if minimal else "false"}
	image_id = AtlasClient.from_settings().find_system_image(tags)
	if not image_id:
		frappe.throw(
			frappe._("Atlas has no available system image tagged {0}.").format(
				", ".join(f"{key}:{value}" for key, value in tags.items())
			)
		)

	return image_id


def host_port(address: str, port: int | str) -> str:
	"""``address:port``, with the brackets an IPv6 address needs to keep its colons."""
	return f"[{address}]:{port}" if ":" in address else f"{address}:{port}"


def error_message(payload: Any, fallback: str) -> str:
	"""The readable message out of an Atlas or Frappe error body."""
	if isinstance(payload, dict):
		error = payload.get("error")
		if isinstance(error, dict) and error.get("message"):
			fields = "; ".join(
				f"{field.get('name')}: {field.get('message')}"
				for field in error.get("fields") or []
				if isinstance(field, dict)
			)
			return f"{error['message']} ({fields})" if fields else str(error["message"])

		for key in ("exception", "exc_type", "message", "_error_message"):
			if payload.get(key):
				return str(payload[key])

	return (fallback or "").strip() or "unknown error"
