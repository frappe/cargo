import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import frappe
import yaml
from frappe.tests import IntegrationTestCase

from cargo.image_builder.api.one_click_image import (
	get_one_click_image_requirements,
	get_supported_one_click_images,
)
from cargo.image_builder.doctype.one_click_image.one_click_image import OneClickImage

APP_CONFIG = """\
title: FastAPI
description: A test app.
documentation: https://docs.example.com/apps/fastapi
base_image: {os: Ubuntu, os_version: '24.04', minimal: true}
minimum_size: {cpu_millicores: 500, memory_mib: 512, disk_mib: 5120}
ports:
  - {port: 80, protocol: tcp}
inputs:
  - {name: domain, label: Domain, type: domain, required: false}
  - {name: admin_email, label: Admin email, type: email, required: true}
  - {name: admin_password, label: Admin password, type: password}
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
		(self.base_path / "fastapi" / "config" / "template.yaml").write_text(APP_CONFIG)

		self.enterContext(patch.object(OneClickImage, "application_base_path", self.base_path))
		self.enterContext(patch.object(OneClickImage, "after_insert"))
		repository = MagicMock(head_sha="commit-1")
		self.enterContext(
			patch(
				"cargo.image_builder.doctype.one_click_image.one_click_image.AppRepository.from_settings",
				return_value=repository,
			)
		)
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
					"documentation": "https://docs.example.com/apps/fastapi",
					"os_version": "24.04",
					"information": "A test app.",
					"logo": None,
					"snapshot_id": "img-new",
				}
			],
		)

	def test_requirements_come_from_the_completed_image_app(self):
		self.image("Completed")

		self.assertEqual(
			get_one_click_image_requirements("fastapi"),
			{
				"inputs": yaml.safe_load(APP_CONFIG)["inputs"],
				"minimum_size": {"cpu_millicores": 500, "memory_mib": 512, "disk_mib": 5120},
				"ports": [{"port": 80, "protocol": "tcp"}],
				"documentation": "https://docs.example.com/apps/fastapi",
			},
		)

	def test_an_application_without_a_completed_image_has_no_requirements(self):
		self.image("Building")

		with self.assertRaises(frappe.DoesNotExistError):
			get_one_click_image_requirements("fastapi")

	def test_an_image_offers_the_app_it_was_built_from(self):
		self.image("Completed")
		(self.base_path / "fastapi" / "config" / "template.yaml").write_text(
			APP_CONFIG.replace("FastAPI", "Renamed")
		)

		self.assertEqual(get_supported_one_click_images()[0]["title"], "FastAPI")
		self.assertEqual(
			get_one_click_image_requirements("fastapi")["inputs"], yaml.safe_load(APP_CONFIG)["inputs"]
		)
