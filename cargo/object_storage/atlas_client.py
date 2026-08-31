from __future__ import annotations

from cargo.atlas_client import AtlasClient as BaseAtlasClient
from cargo.atlas_client import AtlasError
from cargo.object_storage.client_models import PlacementGroupSchema


class AtlasClient(BaseAtlasClient):
	"""Atlas, as object storage asks for it."""

	def create_vms(
		self,
		title: str,
		placement: PlacementGroupSchema,
		*,
		base_image: str = "ubuntu-22.04",
	) -> list[str]:
		"""Ask for a placement group's machines and return their VM ids. They are still
		booting and have no address yet."""
		created = self.call(
			"create_bare_vms",
			title=title,
			base_image=base_image,
			placement_group=placement.asdict(),
			ssh_public_key=self.public_key,
		)
		vm_ids = created.get("vm_ids") if isinstance(created, dict) else created
		if not vm_ids:
			raise AtlasError(0, f"create_bare_vms returned no VM ids: {created!r}")

		return list(vm_ids)
