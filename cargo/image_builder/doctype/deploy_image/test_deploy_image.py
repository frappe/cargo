# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

import tempfile
from pathlib import Path
from unittest.mock import patch

import frappe
from cloudtoolkit.pipeline.utils import called_methods_in_order
from frappe.tests import IntegrationTestCase

from cargo.image_builder.doctype.deploy_image import deploy_image
from cargo.image_builder.doctype.deploy_image.deploy_image import DeployImage, get_agent_hash
from cargo.image_builder.doctype.pilot_image.builder import Builder, get_build_spec
from cargo.testing import use_test_settings

CONTROLLER = "cargo.image_builder.doctype.deploy_image.deploy_image"


class IntegrationTestDeployImage(IntegrationTestCase):
	"""How the push-to-deploy image is built over SSH and captured."""

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("deploy_image_test")
		self.addCleanup(frappe.db.rollback, save_point="deploy_image_test")
		use_test_settings()

	def image(self) -> DeployImage:
		"""An image with a running build machine, without asking Atlas for one."""
		with patch.object(DeployImage, "after_insert"):
			image: DeployImage = frappe.get_doc({"doctype": "Deploy Image"}).insert()
		machine = frappe.get_doc(
			{
				"doctype": "Machine",
				"reference_doctype": image.doctype,
				"reference_name": image.name,
				"role": "builder",
				"disk_size_gb": 10,
				"vm_id": f"vm-{frappe.generate_hash(length=8)}",
				"address": "fdaa:1::1d",
				"status": "Running",
			}
		).insert()
		image.db_set({"machine": machine.name, "status": "Building"})
		image.reload()
		return image

	def test_every_build_step_is_tracked(self):
		flow = frappe.new_doc("Deploy Image")._create_image._wrapped
		self.assertEqual(
			[name for name, _ in called_methods_in_order(DeployImage, flow)],
			["install_packages", "copy_files", "configure", "take_snapshot", "wait_for_snapshot"],
		)

	def test_the_image_is_built_on_the_build_machine_from_cargo_settings(self):
		with (
			patch(f"{CONTROLLER}.get_system_image", return_value="img-ubuntu"),
			patch("cargo.image_builder.doctype.build_machine.Machine.request") as request,
		):
			request.return_value.name = "machine-1"
			frappe.get_doc({"doctype": "Deploy Image"}).insert()
		self.assertEqual(request.call_args.kwargs["spec"], get_build_spec())
		self.assertEqual(request.call_args.kwargs["base_image"], "img-ubuntu")

	def test_central_finds_the_image_by_its_deploy_tag(self):
		self.assertEqual(frappe.new_doc("Deploy Image").get_atlas_tags()["purpose"], "deploy")

	def test_the_agent_packages_are_installed(self):
		image = self.image()
		with (
			patch.object(Builder, "wait_until_reachable"),
			patch("cloudtoolkit.ssh.client.SSHClient.run") as run,
		):
			image.install_packages()
		self.assertIn(
			"apt-get install -y rsync git podman buildah nginx nftables\n",
			run.call_args.args[0],
		)

	def test_the_agent_and_its_units_are_copied_laid_out_as_on_the_vm(self):
		image = self.image()
		copied = []

		def rsync(source, destination, *args, **kwargs):
			copied.extend(str(file.relative_to(source)) for file in source.rglob("*") if file.is_file())

		with patch("cloudtoolkit.ssh.client.SSHClient.rsync", side_effect=rsync):
			image.copy_files()

		for path in (
			"usr/local/lib/deploy-agent/build_agent/__main__.py",
			"usr/local/lib/deploy-agent/build_agent/release.py",
			"usr/local/lib/deploy-agent/build_agent/build.sh",
			"etc/systemd/system/app@.service",
			"etc/systemd/system/deploy-agent.timer",
			"etc/nginx/conf.d/deploy-agent.conf",
			"etc/systemd/system/metadata-block.service",
			"etc/deploy-agent/metadata-block.nft",
			"etc/update-motd.d/60-deploy-app",
		):
			self.assertIn(path, copied)
		self.assertFalse([path for path in copied if "__pycache__" in path])


class IntegrationTestDeployImageRebuild(IntegrationTestCase):
	"""Cargo builds a new image when the agent files change, and retires the images it replaces."""

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("deploy_image_rebuild_test")
		self.addCleanup(frappe.db.rollback, save_point="deploy_image_rebuild_test")
		frappe.db.delete("Deploy Image")
		patch.object(DeployImage, "after_insert").start()
		patch(f"{CONTROLLER}.frappe.db.commit").start()
		self.addCleanup(patch.stopall)

	def image(self, status: str, agent_hash: str = "old") -> str:
		image = frappe.get_doc({"doctype": "Deploy Image"}).insert()
		image.db_set({"status": status, "agent_hash": agent_hash})
		return image.name

	def test_a_new_image_records_the_hash_of_the_files_it_takes(self):
		image = frappe.get_doc({"doctype": "Deploy Image"}).insert()
		self.assertEqual(image.agent_hash, get_agent_hash())

	def test_a_change_to_a_file_the_image_takes_changes_the_hash(self):
		before = get_agent_hash()
		with tempfile.NamedTemporaryFile("w", suffix=".sh") as script:
			script.write("changed\n")
			script.flush()
			with patch.object(deploy_image, "PROVISION_SCRIPT", Path(script.name)):
				self.assertNotEqual(get_agent_hash(), before)

	def test_changed_agent_files_start_a_new_image(self):
		self.image("Completed")
		name = deploy_image.create_new_deploy_images()
		self.assertEqual(frappe.db.get_value("Deploy Image", name, "agent_hash"), get_agent_hash())

	def test_the_first_image_is_started_without_one_built_by_hand(self):
		self.assertIsNotNone(deploy_image.create_new_deploy_images())

	def test_no_image_is_started_while_one_is_building(self):
		self.image("Completed")
		self.image("Building")
		self.assertIsNone(deploy_image.create_new_deploy_images())

	def test_an_image_of_the_current_files_is_not_built_again_even_if_it_failed(self):
		self.image("Completed")
		self.image("Failed", get_agent_hash())
		self.assertIsNone(deploy_image.create_new_deploy_images())

	def test_only_the_newest_completed_image_is_kept(self):
		older = self.image("Completed")
		newer = self.image("Completed")
		building = self.image("Building", get_agent_hash())

		self.assertEqual(deploy_image.retire_older_deploy_images(), [older])
		self.assertEqual(frappe.db.get_value("Deploy Image", newer, "status"), "Completed")
		self.assertEqual(frappe.db.get_value("Deploy Image", building, "status"), "Building")
