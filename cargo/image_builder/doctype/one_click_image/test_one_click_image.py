# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

import os
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from cargo.cargo.doctype.machine.machine import Machine
from cargo.image_builder.doctype.one_click_image.one_click_image import (
	OneClickImage,
	get_system_image,
	refresh_latest_registry,
	report_if_registry_is_stale,
)
from cargo.image_builder.doctype.pilot_image.builder import PROVISION_TIMEOUT, Builder
from cargo.ssh import SshError
from cargo.testing import use_test_settings
from cargo.workflow_engine.utils import called_methods_in_order

CONTROLLER = "cargo.image_builder.doctype.one_click_image.one_click_image"
APP_HEADER = "description: A test app.\nbase_image:\n  os: Ubuntu\n  os_version: '24.04'\n  minimal: true\n"


class IntegrationTestOneClickImage(IntegrationTestCase):
	"""How an application folder is checked, built over SSH and captured."""

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("one_click_image_test")
		self.addCleanup(frappe.db.rollback, save_point="one_click_image_test")
		use_test_settings()

		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.base_path = Path(directory.name)
		self.repository = MagicMock(head_sha="commit-1", path=self.base_path)
		for patcher in (
			patch.object(OneClickImage, "application_base_path", self.base_path),
			patch(f"{CONTROLLER}.get_system_image", return_value="img-ubuntu"),
			patch(f"{CONTROLLER}.AppRepository.from_settings", return_value=self.repository),
			# A refresh commits after each image, which would leak rows past the test's rollback.
			patch.object(frappe.db, "commit"),
		):
			patcher.start()
			self.addCleanup(patcher.stop)

		self.application("common", scripts=("900-cleanup.sh",))

	def application(self, name: str = "wordpress", scripts: tuple[str, ...] = ("install.sh",)) -> Path:
		path = self.base_path / name
		for folder in ("src", "config", "scripts"):
			(path / folder).mkdir(parents=True)
		for script in scripts:
			(path / "scripts" / script).write_text(f"echo {script}\n")
		(path / "config" / "template.yaml").write_text(f"{APP_HEADER}build:\n  files: []\n")
		return path

	def image(self, name: str = "wordpress", status: str = "Provisioning") -> OneClickImage:
		"""An image with a running build machine, without asking Atlas for one."""
		with patch.object(OneClickImage, "after_insert"):
			image: OneClickImage = frappe.get_doc(
				{"doctype": "One Click Image", "application": name}
			).insert()

		machine = frappe.get_doc(
			{
				"doctype": "Machine",
				"reference_doctype": image.doctype,
				"reference_name": image.name,
				"role": "builder",
				"disk_size_gb": 8,
				"vm_id": f"vm-{frappe.generate_hash(length=8)}",
				"address": "fdaa:1::1d",
				"status": "Running",
			}
		).insert()
		image.db_set({"machine": machine.name, "status": status})
		image.reload()
		return image

	def assert_rejected(self, message: str, name: str = "wordpress"):
		with self.assertRaisesRegex(frappe.ValidationError, message):
			frappe.get_doc({"doctype": "One Click Image", "application": name}).insert()

	def test_a_name_that_could_leave_the_base_folder_is_rejected(self):
		self.assert_rejected("lowercase letters", name="../etc")

	def test_common_is_not_an_application(self):
		self.assert_rejected("shared by every application", name="common")

	def test_a_symbolic_link_is_rejected(self):
		path = self.application()
		os.symlink("/etc/passwd", path / "src" / "passwd")
		self.assert_rejected("Symbolic links")

	def test_an_extra_top_level_entry_is_rejected(self):
		(self.application() / "README.md").write_text("")
		self.assert_rejected("README.md")

	def test_an_invalid_package_name_is_rejected(self):
		(self.application() / "config" / "packages.txt").write_text("nginx\nnginx; curl evil\n")
		self.assert_rejected("not a valid apt package name")

	def test_only_shell_scripts_are_allowed(self):
		self.application(scripts=("install.py",))
		self.assert_rejected("Only .sh files")

	def test_a_base_image_atlas_does_not_have_is_rejected(self):
		self.application()
		with patch(f"{CONTROLLER}.get_system_image", return_value=None):
			self.assert_rejected("no available system image")

	def test_the_build_boots_on_the_base_image_from_the_app(self):
		self.application()
		with (
			patch(f"{CONTROLLER}.get_system_image", wraps=get_system_image) as lookup,
			patch("cargo.atlas_client.AtlasClient.from_settings") as atlas,
			patch.object(OneClickImage, "after_insert"),
		):
			atlas.return_value.find_system_image.return_value = "img-minimal"
			image = frappe.get_doc({"doctype": "One Click Image", "application": "wordpress"}).insert()
			self.assertEqual(image.get_boot_image(), "img-minimal")

		lookup.assert_called_once_with("Ubuntu", "24.04", True)
		self.assertEqual(atlas.return_value.find_system_image.call_args.args[0]["minimal"], "true")

	def test_the_app_description_and_os_version_are_saved(self):
		self.application()
		image = self.image()

		self.assertEqual(
			frappe.db.get_value(image.doctype, image.name, ["information", "os_version"]),
			("A test app.", "24.04"),
		)

	def test_every_build_step_is_tracked(self):
		flow = frappe.new_doc("One Click Image")._create_image._wrapped
		self.assertEqual(
			[name for name, _ in called_methods_in_order(OneClickImage, flow)],
			["install_packages", "copy_files", "run_build_scripts", "take_snapshot", "wait_for_snapshot"],
		)

	def test_a_running_machine_starts_the_build(self):
		self.application()
		image = self.image()

		with patch.object(OneClickImage, "create_image") as create_image:
			image.sync_machines()

		create_image.assert_called_once()
		self.assertEqual(image.status, "Building")

	def test_packages_are_installed_without_comments_or_blank_lines(self):
		(self.application() / "config" / "packages.txt").write_text("# web\nnginx\n\npython3-venv  # app\n")
		(self.base_path / "common" / "config" / "packages.txt").write_text("curl\n")
		image = self.image(status="Building")

		with (
			patch.object(Builder, "wait_until_reachable"),
			patch(f"{CONTROLLER}.run_over_ssh") as run_over_ssh,
		):
			image.install_packages()

		self.assertIn(
			"apt-get install -y --no-install-recommends rsync curl nginx python3-venv\n",
			run_over_ssh.call_args.args[1],
		)

	def test_each_folder_is_copied_in_one_rsync_laid_out_as_on_the_machine(self):
		path = self.application()
		(path / "src" / "wp-config.php").write_text("")
		(path / "src" / "unused.txt").write_text("")
		(path / "src" / "plugins").mkdir()
		(path / "src" / "plugins" / "cache.php").write_text("")
		(path / "config" / "template.yaml").write_text(
			f"{APP_HEADER}build:\n  files:\n"
			"    - source: src/wp-config.php\n      destination: /var/www/wp-config.php\n"
			"    - source: src/plugins\n      destination: /var/www/plugins\n"
		)
		(self.base_path / "common" / "config" / "template.yaml").write_text("build:\n  files: []\n")
		image = self.image(status="Building")
		copied = []

		def rsync_over_ssh(address, source, destination, *args, **kwargs):
			files = sorted(str(file.relative_to(source)) for file in source.rglob("*") if file.is_file())
			copied.append((destination, files))

		with patch(f"{CONTROLLER}.rsync_over_ssh", side_effect=rsync_over_ssh):
			image.copy_files()

		self.assertEqual(
			copied,
			[("/", []), ("/", ["var/www/plugins/cache.php", "var/www/wp-config.php"])],
		)

	def test_an_app_source_outside_its_folder_is_rejected(self):
		(self.application() / "config" / "template.yaml").write_text(f"{APP_HEADER}build:\n  files: []\n")
		(self.base_path / "common" / "config" / "template.yaml").write_text(
			"build:\n  files:\n    - source: ../wordpress/config\n      destination: /etc\n"
		)
		image = self.image(status="Building")

		with patch(f"{CONTROLLER}.rsync_over_ssh"), self.assertRaisesRegex(frappe.ValidationError, "inside"):
			image.copy_files()

	def test_an_app_destination_outside_the_machine_is_rejected(self):
		(self.application() / "config" / "template.yaml").write_text(f"{APP_HEADER}build:\n  files: []\n")
		(self.base_path / "common" / "config" / "template.yaml").write_text(
			"build:\n  files:\n    - source: src\n      destination: /../../escaped\n"
		)
		image = self.image(status="Building")

		with (
			patch(f"{CONTROLLER}.rsync_over_ssh") as rsync,
			self.assertRaisesRegex(frappe.ValidationError, "stay inside"),
		):
			image.copy_files()
		rsync.assert_not_called()

	def test_application_scripts_run_before_common_within_one_budget(self):
		self.application(scripts=("20-app.sh", "10-setup.sh"))
		image = self.image(status="Building")

		with patch(f"{CONTROLLER}.run_over_ssh") as run_over_ssh:
			image.run_build_scripts()

		scripts = [call.args[1] for call in run_over_ssh.call_args_list]
		self.assertEqual(scripts, ["echo 10-setup.sh\n", "echo 20-app.sh\n", "echo 900-cleanup.sh\n"])
		self.assertTrue(
			all(call.kwargs["timeout"] <= PROVISION_TIMEOUT for call in run_over_ssh.call_args_list)
		)

	def test_the_snapshot_is_disk_only_and_tagged_with_the_application(self):
		self.application()
		image = self.image(status="Building")

		with (
			patch.object(Builder, "stop_build_machine") as stop,
			patch.object(Machine, "snapshot", return_value="img-wordpress") as snapshot,
		):
			image.take_snapshot()

		self.assertFalse(stop.call_args.kwargs["flush"])
		self.assertFalse(snapshot.call_args.kwargs["memory_snapshot"])
		self.assertEqual(snapshot.call_args.args[1]["application"], "wordpress")
		self.assertEqual(snapshot.call_args.args[1]["os_version"], "24.04")
		self.assertEqual(image.snapshot_id, "img-wordpress")
		self.assertEqual(image.status, "Snapshotting")

	def test_retiring_deletes_the_atlas_image(self):
		self.application()
		image = self.image(status="Completed")

		with patch.object(OneClickImage, "delete_atlas_image") as delete_atlas_image:
			image.retire()

		delete_atlas_image.assert_called_once()
		self.assertEqual(frappe.db.get_value(image.doctype, image.name, "status"), "Retired")

	def test_a_build_in_progress_cannot_be_retired(self):
		self.application()
		image = self.image(status="Building")

		with (
			patch.object(OneClickImage, "delete_atlas_image") as delete_atlas_image,
			self.assertRaises(frappe.ValidationError),
		):
			image.retire()

		delete_atlas_image.assert_not_called()

	def test_an_image_is_pinned_to_the_commit_and_keeps_its_app_config(self):
		self.application()
		image = self.image()

		self.assertEqual(image.commit, "commit-1")
		self.assertEqual(image.app["description"], "A test app.")

	def test_the_logo_is_kept_as_a_data_uri(self):
		path = self.application()
		(path / "config" / "template.yaml").write_text(
			f"{APP_HEADER}logo: config/logo.svg\nbuild:\n  files: []\n"
		)
		(path / "config" / "logo.svg").write_text(
			'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1"/></svg>'
		)

		self.assertTrue(self.image().logo.startswith("data:image/svg+xml;base64,"))

	def test_an_unsafe_or_misplaced_logo_is_rejected(self):
		path = self.application()
		(path / "config" / "logo.svg").write_text("<svg><script>alert(1)</script></svg>")
		for logo, message in (
			("config/logo.svg", "without scripts"),
			("../common/config/logo.svg", "inside the config folder"),
			("config/missing.svg", "does not exist"),
		):
			with self.subTest(logo=logo):
				(path / "config" / "template.yaml").write_text(
					f"{APP_HEADER}logo: {logo}\nbuild:\n  files: []\n"
				)
				self.assert_rejected(message)

	def test_a_new_image_uses_the_existing_clone(self):
		self.application()

		self.image()

		self.repository.clone.assert_not_called()
		self.repository.reject_if_tampered.assert_called()

	def test_the_first_image_clones_the_repository(self):
		self.application()
		self.repository.is_cloned = False

		self.image()

		self.repository.clone.assert_called_once_with()

	def test_a_refresh_builds_each_application_without_an_image(self):
		self.application()
		self.image(status="Completed")
		self.application("redis")
		(self.base_path / "_template").mkdir()

		with patch.object(OneClickImage, "after_insert"):
			refresh_latest_registry()

		self.repository.refresh.assert_called_once_with()
		self.assertEqual(
			frappe.get_all("One Click Image", pluck="application", order_by="application"),
			["redis", "wordpress"],
		)

	def test_a_refresh_waits_while_an_image_builds(self):
		self.application()
		self.image(status="Building")
		self.application("redis")

		refresh_latest_registry()

		self.repository.refresh.assert_not_called()
		self.assertFalse(frappe.db.exists("One Click Image", {"application": "redis"}))

	def test_a_failed_application_does_not_stop_the_others(self):
		self.application("redis")
		self.application("valkey")
		(self.base_path / "redis" / "README.md").write_text("")

		with (
			patch.object(OneClickImage, "after_insert"),
			patch.object(frappe.db, "rollback") as rollback,
			patch.object(frappe, "log_error") as log_error,
		):
			refresh_latest_registry()

		self.assertEqual(frappe.get_all("One Click Image", pluck="application"), ["valkey"])
		rollback.assert_called_once_with()
		self.assertIn("redis", log_error.call_args.kwargs["title"])

	def test_a_build_does_not_start_once_the_clone_moved_off_its_commit(self):
		self.application()
		image = self.image(status="Building")
		self.repository.head_sha = "commit-2"

		with (
			patch.object(Builder, "wait_until_reachable") as wait,
			self.assertRaisesRegex(frappe.ValidationError, "commit-1"),
		):
			image.install_packages()

		wait.assert_not_called()

	def test_a_repository_not_refreshed_for_6_hours_is_logged(self):
		self.application()
		self.image(status="Building")
		for hours, is_logged in ((None, False), (5, False), (7, True)):
			last_refresh = now_datetime() - timedelta(hours=hours) if hours else None
			frappe.db.set_single_value("Cargo Settings", "last_repository_refresh", last_refresh)

			with self.subTest(hours=hours), patch.object(frappe, "log_error") as log_error:
				report_if_registry_is_stale()

			self.assertEqual(log_error.called, is_logged)

		self.assertEqual(log_error.call_args.kwargs["title"], "One-click apps repository is stale")
		self.assertIn("OCI-", log_error.call_args.kwargs["message"])
