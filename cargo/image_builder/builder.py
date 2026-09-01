import os
import shlex
import subprocess
from pathlib import Path


class Builder:
	"""Builds one Firecracker rootfs on this host: one flavour of one pilot release."""

	def __init__(
		self, pilot_version: str, frappe_version: str | None = None, site: str | None = None
	) -> None:
		"""A blank dimension does not apply to this image: no Frappe, or no notion of a site."""
		self.pilot_version = pilot_version
		self.frappe_version = frappe_version or ""
		self.site = site or ""

	def run(self, command: str | list[str]) -> subprocess.CompletedProcess:
		"""Raises CalledProcessError on a non-zero exit; its stderr carries the reason."""
		return subprocess.run(
			command if isinstance(command, list) else shlex.split(command),
			check=True,
			capture_output=True,
			text=True,
		)

	@property
	def has_kvm(self) -> bool:
		"""Check if the kvm module is loaded on the host."""
		return os.access("/dev/kvm", os.R_OK | os.W_OK)

	def install_dependencies(self) -> None:
		"""Packages the rootfs build needs, beyond what the Cargo host already has."""
		...

	def build_rootfs(self) -> Path:
		"""Assemble the filesystem tree and pack it into an ext4 image."""
		...

	def build(self) -> Path:
		"""The built image, ready to upload."""
		if not self.has_kvm:
			raise RuntimeError("/dev/kvm is not available on this host.")

		self.install_dependencies()

		return self.build_rootfs()
