import io
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

import frappe
from frappe import _

from cargo.image_builder.doctype.one_click_image.one_click_image_validator import APPLICATION_NAME, COMMON

REFRESH_INTERVAL_SECONDS = 60 * 60
LS_REMOTE_TIMEOUT_SECONDS = 15
GIT_TIMEOUT_SECONDS = 300
BRANCH_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


class TemplateRepositoryError(frappe.ValidationError):
	pass


class TemplateRepository:
	"""Shallow, read-only clone of the one-click templates repository, refreshed hourly like
	Pilot's marketplace registry cache."""

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
	def last_checked_path(self) -> Path:
		return self.path.with_name(f"{self.path.name}.last_checked")

	@property
	def head_sha(self) -> str:
		return self.git("rev-parse", "HEAD")

	def ensure_fresh(self, force: bool = False) -> None:
		"""Clone on first use. Later, refuse a hand-edited clone and refresh once the last check
		is older than an hour, or at once with `force`."""
		if not (self.path / ".git").is_dir():
			self.clone()
			self.last_checked_path.touch()
			return

		self.reject_if_tampered()
		if force or self.is_refresh_due:
			self.refresh()
			self.last_checked_path.touch()

	@property
	def is_refresh_due(self) -> bool:
		if not self.last_checked_path.exists():
			return True

		return time.time() - self.last_checked_path.stat().st_mtime >= REFRESH_INTERVAL_SECONDS

	def clone(self) -> None:
		self.path.parent.mkdir(parents=True, exist_ok=True)
		run(["git", "clone", "--depth", "1", "--branch", self.branch, "--", self.url, str(self.path)])

	def refresh(self) -> None:
		"""Move to the newest commit of the branch. An unreachable GitHub keeps the clone as it is."""
		remote_head = self.get_remote_head_sha()
		if not remote_head or remote_head == self.head_sha:
			return

		try:
			self.git("fetch", "--depth", "1", "origin", self.branch)
			self.git("reset", "--hard", "FETCH_HEAD")
		except TemplateRepositoryError:
			frappe.log_error(title="One-click templates could not be refreshed")

	def get_remote_head_sha(self) -> str | None:
		try:
			output = run(
				["git", "ls-remote", self.url, f"refs/heads/{self.branch}"], timeout=LS_REMOTE_TIMEOUT_SECONDS
			)
		except (TemplateRepositoryError, subprocess.TimeoutExpired):
			return None

		return output.split("\t", 1)[0] or None

	def reject_if_tampered(self) -> None:
		if self.git("status", "--porcelain"):
			frappe.throw(
				_(
					"The one-click templates clone at {0} was changed by hand. Delete it so Cargo clones it again."
				).format(self.path),
				TemplateRepositoryError,
			)

	def list_applications(self) -> list[str]:
		"""The templates at the current commit: every top-level folder named like an application."""
		folders = self.git("ls-tree", "-d", "--name-only", "HEAD").splitlines()
		return [folder for folder in folders if folder != COMMON and APPLICATION_NAME.match(folder)]

	def get_source_hash(self, application: str) -> str:
		"""Changes when the application's folder or `common` changes, and not otherwise."""
		return f"{self.git('rev-parse', f'HEAD:{application}')}:{self.git('rev-parse', f'HEAD:{COMMON}')}"

	def export(self, destination: Path) -> str:
		"""Write the files of the current commit to `destination` once, and return the commit."""
		commit = self.head_sha
		if destination.is_dir():
			return commit

		archive = subprocess.run(
			["git", "-C", str(self.path), "archive", commit],
			capture_output=True,
			timeout=GIT_TIMEOUT_SECONDS,
			check=True,
		).stdout
		staging = destination.with_name(f"{destination.name}.partial")
		shutil.rmtree(staging, ignore_errors=True)
		staging.mkdir(parents=True)
		# The data filter refuses absolute paths, `..` and links that leave the folder.
		with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
			tar.extractall(staging, filter="data")
		staging.rename(destination)
		return commit

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
