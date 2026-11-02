import re
import subprocess
from pathlib import Path

import frappe
from frappe import _

GIT_TIMEOUT_SECONDS = 300
BRANCH_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


class TemplateRepositoryError(frappe.ValidationError):
	pass


class TemplateRepository:
	"""The one shallow, read-only clone of the one-click templates repository, made on first use."""

	def __init__(self, url: str, branch: str, path: Path):
		self.url = url
		self.branch = branch
		self.path = path

	@classmethod
	def from_settings(cls) -> "TemplateRepository":
		url = frappe.db.get_single_value("Cargo Settings", "one_click_repository_url")
		branch = frappe.db.get_single_value("Cargo Settings", "one_click_repository_branch") or "main"
		if not url or not url.startswith("https://"):
			frappe.throw(_("Set an https:// One Click Repository URL in Cargo Settings."))
		if not BRANCH_NAME.fullmatch(branch):
			frappe.throw(_("{0} is not a branch name.").format(branch))

		return cls(url, branch, Path(frappe.get_site_path("private", "one-click-templates")))

	@property
	def is_cloned(self) -> bool:
		return (self.path / ".git").is_dir()

	@property
	def head_sha(self) -> str:
		return self.git("rev-parse", "HEAD")

	def clone(self) -> None:
		self.path.parent.mkdir(parents=True, exist_ok=True)
		run(["git", "clone", "--depth", "1", "--branch", self.branch, "--", self.url, str(self.path)])

	def refresh(self) -> None:
		self.git("fetch", "--depth", "1", "origin", self.branch)
		self.git("reset", "--hard", "FETCH_HEAD")

	def reject_if_tampered(self) -> None:
		if self.git("status", "--porcelain"):
			frappe.throw(
				_(
					"The one-click templates clone at {0} was changed by hand. Delete it so Cargo clones it again."
				).format(self.path),
				TemplateRepositoryError,
			)

	def git(self, *args: str) -> str:
		return run(["git", "-C", str(self.path), *args]).strip()


def run(command: list[str], timeout: int = GIT_TIMEOUT_SECONDS) -> str:
	result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
	if result.returncode != 0:
		frappe.throw(
			_("{0} failed: {1}").format(" ".join(command[:3]), result.stderr.strip()[-500:]),
			TemplateRepositoryError,
		)

	return result.stdout
