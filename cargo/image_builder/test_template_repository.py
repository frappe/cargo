import subprocess
import tempfile
from pathlib import Path

import frappe
from frappe.tests import IntegrationTestCase

from cargo.image_builder.template_repository import TemplateRepository, TemplateRepositoryError

GIT_AUTHOR = ["-c", "user.name=Cargo Test", "-c", "user.email=cargo@example.test"]


class IntegrationTestTemplateRepository(IntegrationTestCase):
	"""The clone of the one-click templates repository, against a local origin."""

	def setUp(self):
		self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
		self.origin = self.root / "origin"
		self.git(self.origin.parent, "init", "--quiet", "--initial-branch", "main", str(self.origin))
		for folder in ("common", "redis", "mariadb", "_template"):
			self.write(f"{folder}/config/template.yaml", f"name: {folder}\n")
		self.commit("Add templates")
		self.repository = TemplateRepository(f"file://{self.origin}", "main", self.root / "clone")

	def git(self, directory: Path, *args: str) -> str:
		return subprocess.run(
			["git", "-C", str(directory), *GIT_AUTHOR, *args], capture_output=True, text=True, check=True
		).stdout.strip()

	def write(self, path: str, content: str) -> None:
		(self.origin / path).parent.mkdir(parents=True, exist_ok=True)
		(self.origin / path).write_text(content)

	def commit(self, message: str) -> str:
		self.git(self.origin, "add", "--all")
		self.git(self.origin, "commit", "--quiet", "--message", message)
		return self.git(self.origin, "rev-parse", "HEAD")

	def test_the_first_use_clones_and_lists_the_templates(self):
		self.repository.ensure_fresh()

		self.assertEqual(self.repository.head_sha, self.git(self.origin, "rev-parse", "HEAD"))
		self.assertEqual(self.repository.list_applications(), ["mariadb", "redis"])

	def test_an_export_holds_the_files_of_the_commit(self):
		self.repository.ensure_fresh()
		destination = self.root / "exports" / "build"

		commit = self.repository.export(destination)

		self.assertEqual(commit, self.repository.head_sha)
		self.assertEqual((destination / "redis" / "config" / "template.yaml").read_text(), "name: redis\n")
		self.assertFalse((destination / ".git").exists())

	def test_a_refresh_moves_to_the_new_commit_and_changes_only_that_template(self):
		self.repository.ensure_fresh()
		redis, mariadb = (self.repository.get_source_hash(name) for name in ("redis", "mariadb"))
		self.write("redis/config/template.yaml", "name: redis\ntitle: Redis\n")
		head = self.commit("Change Redis")

		self.repository.ensure_fresh(force=True)

		self.assertEqual(self.repository.head_sha, head)
		self.assertNotEqual(self.repository.get_source_hash("redis"), redis)
		self.assertEqual(self.repository.get_source_hash("mariadb"), mariadb)

	def test_a_change_to_common_changes_every_template(self):
		self.repository.ensure_fresh()
		redis = self.repository.get_source_hash("redis")
		self.write("common/config/packages.txt", "curl\n")
		self.commit("Change common")

		self.repository.ensure_fresh(force=True)

		self.assertNotEqual(self.repository.get_source_hash("redis"), redis)

	def test_an_unreachable_origin_keeps_the_clone(self):
		self.repository.ensure_fresh()
		head = self.repository.head_sha
		self.repository.url = f"file://{self.root / 'gone'}"

		self.repository.ensure_fresh(force=True)

		self.assertEqual(self.repository.head_sha, head)

	def test_a_hand_edited_clone_is_refused(self):
		self.repository.ensure_fresh()
		(self.repository.path / "redis" / "config" / "template.yaml").write_text("name: changed\n")

		with self.assertRaises(TemplateRepositoryError):
			self.repository.ensure_fresh()

	def test_only_an_https_repository_is_accepted_from_settings(self):
		frappe.db.set_single_value("Cargo Settings", "one_click_repository_url", "file:///tmp/templates")
		self.addCleanup(frappe.db.rollback)

		with self.assertRaises(frappe.ValidationError):
			TemplateRepository.from_settings()
