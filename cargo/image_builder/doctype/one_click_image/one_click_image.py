# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt
import shlex
import shutil
import tempfile
import time
from functools import cached_property
from pathlib import Path, PurePath

import frappe
import yaml

from cargo.image_builder.doctype.build_machine import BuildMachineOwner
from cargo.image_builder.doctype.one_click_image.one_click_image_validator import (
	COMMON,
	OneClickApplicationValidator,
	get_packages,
)
from cargo.image_builder.doctype.pilot_image.builder import (
	PING_TIMEOUT,
	PROVISION_TIMEOUT,
	SSH_READY_TIMEOUT,
	Builder,
)
from cargo.ssh import OutputLog, rsync_over_ssh, run_over_ssh
from cargo.workflow_engine.doctype.press_workflow.decorators import flow, task
from cargo.workflow_engine.doctype.press_workflow.workflow_builder import WorkflowBuilder

BUILD_TIMEOUT = PING_TIMEOUT + SSH_READY_TIMEOUT + PROVISION_TIMEOUT


class OneClickImage(BuildMachineOwner, WorkflowBuilder):
	# common/scripts/900-cleanup.sh removes the build key and SSH host keys, then syncs the disk.
	has_ssh_after_build = False

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		application: DF.Data | None
		build_log: DF.Code | None
		built_at: DF.Datetime | None
		error: DF.LongText | None
		machine: DF.Link | None
		path: DF.Data | None
		snapshot_id: DF.Data | None
		status: DF.Literal["Provisioning", "Building", "Snapshotting", "Completed", "Failed"]
	# end: auto-generated types

	def before_insert(self):
		"""Most of these validations must be triggered from GitHub actions directly
		before the pull request is merged for this one click image, will write automation for that."""
		OneClickApplicationValidator(self.application, self.application_base_path).validate()

	def after_insert(self):
		"""Start the build process for the one click image, rent a machine for the build."""
		self.request_build_machine()

	@cached_property
	def application_base_path(self) -> Path:
		"""The folder that holds every one click application. A local checkout until Cargo clones
		the GitHub repository."""
		base_path = frappe.db.get_single_value("Cargo Settings", "one_click_image_base_path")
		if not base_path:
			frappe.throw(frappe._("Set One Click Image Base Path in Cargo Settings."))

		return Path(base_path)

	@cached_property
	def application_config(self):
		return yaml.safe_load(
			(self.application_base_path / self.application / "config" / "template.yaml").read_text()
		)

	@cached_property
	def common_config(self):
		return yaml.safe_load((self.application_base_path / COMMON / "config" / "template.yaml").read_text())

	def get_boot_image(self) -> str:
		"""Looked up on every call, so a retry boots on the system image Atlas has now."""
		base_image = self.application_config["base_image"]
		image_id = get_system_image(
			base_image["os"], base_image["os_version"], base_image.get("minimal", False)
		)
		if not image_id:
			frappe.throw(
				frappe._("Atlas has no available system image for {0} {1}.").format(
					base_image["os"], base_image["os_version"]
				)
			)

		return image_id

	def create_image(self):
		"""Called by `sync_machines` once the build machine is running."""
		self._create_image.run_as_workflow()

	@property
	def folders(self) -> list[str]:
		"""What the image is built from, in order: `common`, then the application."""
		return [COMMON, self.application]

	@flow
	def _create_image(self):
		self.install_packages()
		self.copy_files()
		self.run_build_scripts()
		self.take_snapshot()
		self.wait_for_snapshot()

	@task(queue="long", timeout=BUILD_TIMEOUT)
	def install_packages(self):
		"""Install the apt packages first, so the files and scripts after it can rely on them."""
		machine = self.build_machine
		private_key = machine.get_password("ssh_private_key")
		Builder().wait_until_reachable(machine.address, private_key)

		# Rsync is required for the copy_files step
		base_path = self.application_base_path
		packages = [
			"rsync",
			*(package for folder in self.folders for package in get_packages(folder, base_path)),
		]
		script = (
			"export DEBIAN_FRONTEND=noninteractive\n"
			"cloud-init status --wait > /dev/null || true\n"
			"apt-get update\n"
			"apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold full-upgrade\n"
			f"apt-get install -y --no-install-recommends {shlex.join(packages)}\n"
		)

		with OutputLog(self, "build_log", append=True) as log:
			log.write("==> apt packages\n")
			self.run_on_build_machine(script, log)

	@task(queue="long", timeout=PROVISION_TIMEOUT)
	def copy_files(self):
		"""Copy common files and then the application files"""
		machine = self.build_machine
		for folder in self.folders:
			folder_path = (self.application_base_path / folder).resolve()
			configuration = self.common_config if folder == COMMON else self.application_config

			# Since rsync is copying the contents of the folder and not the folder itself / works.
			with OutputLog(self, "build_log", append=True) as log, tempfile.TemporaryDirectory() as staging:
				log.write(f"==> {folder} files\n")
				for file in configuration["build"]["files"]:
					source = (folder_path / file["source"]).resolve()
					# A template could name `../` and copy Cargo's own files to the build machine.
					if not source.is_relative_to(folder_path) or not source.exists():
						frappe.throw(
							frappe._("{0} is not a file or folder inside {1}.").format(source, folder_path)
						)
					if not PurePath(file["destination"]).is_absolute():
						frappe.throw(
							frappe._("Destination {0} must be an absolute path.").format(file["destination"])
						)

					log.write(f"{file['source']} -> {file['destination']}\n")
					target = Path(staging) / file["destination"].lstrip("/")
					target.parent.mkdir(parents=True, exist_ok=True)
					shutil.copytree(source, target, dirs_exist_ok=True) if source.is_dir() else shutil.copy2(
						source, target
					)

				rsync_over_ssh(
					machine.address,
					Path(staging),
					"/",
					machine.get_password("ssh_private_key"),
					timeout=PROVISION_TIMEOUT,
					on_output=log.write,
				)

	@task(queue="long", timeout=BUILD_TIMEOUT)
	def run_build_scripts(self):
		"""Run the scripts present in application/scripts on the build machine in order"""
		# The scripts share one budget, so SSH times out cleanly before the task is killed.
		deadline = time.monotonic() + PROVISION_TIMEOUT
		scripts_path = (
			self.application_base_path / self.application / "scripts",
			self.application_base_path / "common" / "scripts",
		)
		for script_path in scripts_path:
			for script in sorted(script_path.glob("*.sh")):
				with OutputLog(self, "build_log") as log:
					log.write(f"==> Running {script.name}\n")
					self.run_on_build_machine(
						script.read_text(), log, timeout=max(1, int(deadline - time.monotonic()))
					)

	def run_on_build_machine(self, script: str, log: OutputLog, timeout: int = PROVISION_TIMEOUT) -> None:
		machine = self.build_machine
		run_over_ssh(
			machine.address,
			script,
			machine.get_password("ssh_private_key"),
			timeout=timeout,
			on_output=log.write,
		)

	def get_atlas_tags(self) -> dict[str, str]:
		"""What a search at Atlas asks for to find this application's image."""
		return {
			"purpose": "one-click",
			"application": self.application,
			"os": self.application_config["base_image"]["os"],
			"os_version": self.application_config["base_image"]["os_version"],
		}


def get_system_image(os: str, os_version: str, minimal: bool = False) -> str | None:
	"""The system image a Cargo machine boots on, or None when Atlas has none."""
	from cargo.atlas_client import AtlasClient

	return AtlasClient.from_settings().find_system_image(
		{"purpose": "base", "os": os, "os_version": os_version, "minimal": "true" if minimal else "false"}
	)
