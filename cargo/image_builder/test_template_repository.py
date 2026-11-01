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

	def test_a_clone_is_at_the_head_of_the_branch(self):
		self.repository.clone()

		self.assertTrue(self.repository.is_cloned)
		self.assertEqual(self.repository.head_sha, self.git(self.origin, "rev-parse", "HEAD"))
		self.assertEqual(
			(self.repository.path / "redis" / "config" / "template.yaml").read_text(), "name: redis\n"
		)

	def test_a_hand_edited_clone_is_refused(self):
		self.repository.clone()
		self.repository.reject_if_tampered()
		(self.repository.path / "redis" / "config" / "template.yaml").write_text("name: changed\n")

		with self.assertRaises(TemplateRepositoryError):
			self.repository.reject_if_tampered()

	def test_only_an_https_repository_is_accepted_from_settings(self):
		frappe.db.set_single_value("Cargo Settings", "one_click_repository_url", "file:///tmp/templates")
		self.addCleanup(frappe.db.rollback)

		with self.assertRaises(frappe.ValidationError):
			TemplateRepository.from_settings()
