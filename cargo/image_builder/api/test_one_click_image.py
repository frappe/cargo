import tempfile
from pathlib import Path
from unittest.mock import patch

import frappe
import yaml
from frappe.tests import IntegrationTestCase

from cargo.image_builder.api.one_click_image import (
	get_one_click_image_requirements,
	get_one_click_image_user_data,
	get_supported_one_click_images,
)
from cargo.image_builder.doctype.one_click_image.one_click_image import OneClickImage

TEMPLATE = """\
title: FastAPI
description: A test app.
base_image: {os: Ubuntu, os_version: '24.04', minimal: true}
minimum_size: {cpu_millicores: 500, memory_mib: 512, disk_mib: 5120}
ports:
  - {port: 80, protocol: tcp}
inputs:
  - {name: domain, label: Domain, type: domain, required: false}
  - {name: admin_email, label: Admin email, type: email, required: true}
  - {name: admin_password, label: Admin password, type: password, generate: true}
build: {files: []}
"""


class IntegrationTestOneClickImageApi(IntegrationTestCase):
	"""The calls Central makes to offer and launch one click images."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.addCleanup(frappe.db.rollback)
		self.base_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
		for folder in ("common", "fastapi"):
			for subfolder in ("src", "config", "scripts"):
				(self.base_path / folder / subfolder).mkdir(parents=True)
			(self.base_path / folder / "scripts" / "10-setup.sh").write_text("true\n")
		(self.base_path / "fastapi" / "config" / "template.yaml").write_text(TEMPLATE)

		self.enterContext(patch.object(OneClickImage, "application_base_path", self.base_path))
		self.enterContext(patch.object(OneClickImage, "after_insert"))
		self.enterContext(
			patch(
				"cargo.image_builder.doctype.one_click_image.one_click_image.get_system_image",
				return_value="img-ubuntu",
			)
		)
		self.enterContext(
			patch("cargo.auth.authenticate_request", return_value=frappe._dict(aud="central-admin"))
		)

	def image(self, status: str, snapshot_id: str | None = None) -> OneClickImage:
		image = frappe.get_doc({"doctype": "One Click Image", "application": "fastapi"}).insert()
		image.db_set({"status": status, "snapshot_id": snapshot_id})
		return image

	def test_only_the_newest_completed_image_of_an_application_is_offered(self):
		self.image("Completed", "img-old")
		self.image("Completed", "img-new")
		self.image("Building", "img-building")

		self.assertEqual(
			get_supported_one_click_images(),
			[
				{
					"application": "fastapi",
					"title": "FastAPI",
					"os_version": "24.04",
					"information": "A test app.",
					"snapshot_id": "img-new",
				}
			],
		)

	def test_requirements_come_from_the_completed_image_template(self):
		self.image("Completed")

		self.assertEqual(
			get_one_click_image_requirements("fastapi"),
			{
				"inputs": yaml.safe_load(TEMPLATE)["inputs"],
				"minimum_size": {"cpu_millicores": 500, "memory_mib": 512, "disk_mib": 5120},
				"ports": [{"port": 80, "protocol": "tcp"}],
			},
		)

	def test_an_application_without_a_completed_image_has_no_requirements(self):
		self.image("Building")

		with self.assertRaises(frappe.DoesNotExistError):
			get_one_click_image_requirements("fastapi")

	def test_answers_are_written_to_inputs_env_shell_quoted(self):
		self.image("Completed")
		password = "it's $(reboot) #1"

		user_data = get_one_click_image_user_data(
			"fastapi",
			{"domain": "app.example.com", "admin_email": "me@example.com", "admin_password": password},
		)

		self.assertTrue(user_data.startswith("#cloud-config\n"))
		[file] = yaml.safe_load(user_data)["write_files"]
		self.assertEqual((file["path"], file["permissions"]), ("/etc/one-click/inputs.env", "0600"))
		self.assertEqual(
			file["content"],
			"DOMAIN=app.example.com\nADMIN_EMAIL=me@example.com\nADMIN_PASSWORD='it'\"'\"'s $(reboot) #1'\n",
		)

	def test_a_blank_generated_input_is_left_for_the_vm(self):
		self.image("Completed")

		user_data = get_one_click_image_user_data("fastapi", '{"admin_email": "me@example.com"}')

		self.assertEqual(
			yaml.safe_load(user_data)["write_files"][0]["content"], "ADMIN_EMAIL=me@example.com\n"
		)

	def test_invalid_answers_are_rejected(self):
		self.image("Completed")
		email = {"admin_email": "me@example.com"}

		for answers in (
			{},
			{"admin_email": "not an email"},
			{**email, "domain": "not a domain"},
			{**email, "admin_password": "short"},
			{**email, "admin_password": "a long line\nwith a break"},
			{**email, "shell": "rm -rf /"},
		):
			with self.subTest(answers=answers), self.assertRaises(frappe.ValidationError):
				get_one_click_image_user_data("fastapi", answers)
