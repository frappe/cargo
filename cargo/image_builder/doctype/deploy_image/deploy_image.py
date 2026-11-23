# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt
import shlex
import shutil
import tempfile
from pathlib import Path

import frappe
from cloudtoolkit.pipeline.doctype.pipeline.decorators import flow, task
from cloudtoolkit.pipeline.doctype.pipeline.pipeline_builder import PipelineBuilder
from cloudtoolkit.ssh.output_log import OutputLog
from frappe.utils.change_log import get_app_last_commit_ref

from cargo.image_builder.doctype.build_machine import BuildMachineOwner
from cargo.image_builder.doctype.one_click_image.one_click_image import BUILD_TIMEOUT, get_system_image
from cargo.image_builder.doctype.pilot_image.builder import PROVISION_TIMEOUT, Builder

AGENT_FOLDER = Path(__file__).parents[2] / "conf" / "build_agent"
PROVISION_SCRIPT = Path(__file__).with_name("provision.sh")
# Fow now we hardcode once we have more we can create and keep, and allow
# Users to define via frappe_cloud_deploy.toml
OS, OS_VERSION = "Ubuntu", "24.04"
PACKAGES = ("rsync", "git", "podman", "buildah", "nginx", "nftables")
# Where each agent file lives on the VM, relative to /.
FILES = {
	"usr/local/lib/deploy-agent/build_agent": AGENT_FOLDER,
	"etc/systemd/system/app@.service": AGENT_FOLDER / "app@.service",
	"etc/systemd/system/deploy-agent.service": AGENT_FOLDER / "deploy-agent.service",
	"etc/systemd/system/deploy-agent.timer": AGENT_FOLDER / "deploy-agent.timer",
	"etc/nginx/conf.d/deploy-agent.conf": AGENT_FOLDER / "nginx.conf",
	"etc/systemd/system/metadata-block.service": AGENT_FOLDER / "metadata-block.service",
	"etc/deploy-agent/metadata-block.nft": AGENT_FOLDER / "metadata-block.nft",
}


class DeployImage(BuildMachineOwner, PipelineBuilder):
	"""The image Central boots push-to-deploy VMs from: the deploy agent, podman, buildah and nginx."""

	# provision.sh removes the build key and SSH host keys, then syncs the disk.
	has_ssh_after_build = False

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		build_log: DF.Code | None
		built_at: DF.Datetime | None
		commit: DF.Data | None
		error: DF.LongText | None
		machine: DF.Link | None
		snapshot_id: DF.Data | None
		status: DF.Literal["Provisioning", "Building", "Snapshotting", "Completed", "Failed", "Retired"]
	# end: auto-generated types

	def before_insert(self):
		"""The Cargo commit whose agent the image carries."""
		self.commit = get_app_last_commit_ref("cargo")

	def after_insert(self):
		self.request_build_machine()

	def get_boot_image(self) -> str:
		image_id = get_system_image(OS, OS_VERSION, minimal=True)
		if not image_id:
			frappe.throw(frappe._("Atlas has no available system image for {0} {1}.").format(OS, OS_VERSION))
		return image_id

	def get_atlas_tags(self) -> dict[str, str]:
		"""Central boots the newest available image tagged `purpose: deploy`."""
		return {"purpose": "deploy", "os": OS, "os_version": OS_VERSION}

	def create_image(self):
		"""Called by `sync_machines` once the build machine is running."""
		self._create_image.run_as_pipeline()

	@flow
	def _create_image(self):
		self.install_packages()
		self.copy_files()
		self.configure()
		self.take_snapshot()
		self.wait_for_snapshot()

	@task(queue="long", timeout=BUILD_TIMEOUT)
	def install_packages(self):
		"""Installed before the files, so nginx's install does not start on a config it cannot load yet."""
		machine = self.build_machine
		Builder().wait_until_reachable(machine.address, machine.get_password("ssh_private_key"))
		script = (
			"export DEBIAN_FRONTEND=noninteractive\n"
			"cloud-init status --wait > /dev/null || true\n"
			"apt-get update\n"
			"apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold full-upgrade\n"
			f"apt-get install -y {shlex.join(PACKAGES)}\n"
		)
		with OutputLog(self, "build_log", append=True) as log:
			log.write("==> apt packages\n")
			self.run_on_build_machine(script, log)

	@task(queue="long", timeout=PROVISION_TIMEOUT)
	def copy_files(self):
		"""Copy the agent and its units in one rsync, laid out as on the VM."""
		with OutputLog(self, "build_log", append=True) as log, tempfile.TemporaryDirectory() as staging:
			log.write("==> deploy agent files\n")
			for destination, source in FILES.items():
				target = Path(staging) / destination
				target.parent.mkdir(parents=True, exist_ok=True)
				if source.is_dir():
					shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
				else:
					shutil.copy2(source, target)
			self.build_machine.ssh_client.rsync(
				Path(staging), "/", timeout_seconds=PROVISION_TIMEOUT, on_output=log.write
			)

	@task(queue="long", timeout=PROVISION_TIMEOUT)
	def configure(self):
		"""Enable nginx and the agent's timer, then clean what must differ between VMs."""
		with OutputLog(self, "build_log", append=True) as log:
			log.write("==> provision.sh\n")
			self.run_on_build_machine(PROVISION_SCRIPT.read_text(), log)

	@task(queue="long", timeout=3600)
	def take_snapshot(self) -> None:
		"""Stop the build machine, then ask Atlas to capture its disk."""
		super().take_snapshot()

	@task(queue="short")
	def wait_for_snapshot(self) -> None:
		"""Wait for Atlas to make the image."""
		super().wait_for_snapshot()

	@frappe.whitelist()
	def retire(self) -> None:
		"""Retire this image from the form, so Central stops booting new VMs from it."""
		self.check_permission("write")
		super().retire()

	def run_on_build_machine(self, script: str, log: OutputLog, timeout: int = PROVISION_TIMEOUT) -> None:
		self.build_machine.ssh_client.run(script, timeout_seconds=timeout, on_output=log.write)
