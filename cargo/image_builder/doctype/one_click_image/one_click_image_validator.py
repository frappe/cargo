import re
from pathlib import Path

import frappe

REQUIRED_FOLDERS = ["src", "config", "scripts"]
# Shared by every application: its files go on first and its scripts run last, like cleanup.
COMMON = "common"
PACKAGES_FILE = Path("config") / "packages.txt"
APPLICATION_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")


class OneClickApplicationValidator:
	"""Checks an application folder, and `common`, before an image is built from them."""

	def __init__(self, application: str, base_path: Path):
		self.application = application
		self.base_path = base_path

	def validate(self):
		self.validate_application_name()
		for folder in [COMMON, self.application]:
			self.validate_file_structure(folder)
			self.validate_packages(folder)
			self.validate_script_files(folder)

	def validate_application_name(self):
		"""The name becomes a folder path, so it cannot hold `/`, `..` or anything but lowercase
		letters, digits and hyphens."""
		if not self.application or not APPLICATION_NAME.match(self.application):
			frappe.throw(
				frappe._("Application {0} must use only lowercase letters, digits and hyphens.").format(
					self.application
				)
			)

		if self.application == COMMON:
			frappe.throw(frappe._("{0} is shared by every application and is not one itself.").format(COMMON))

	def validate_file_structure(self, application):
		"""Validate the file structure for the one click image."""
		full_path = self.base_path / application
		if not full_path.is_dir():
			frappe.throw(
				frappe._("The application path {0} does not exist or is not a directory.").format(full_path)
			)

		# A link could point outside the repository and copy Cargo's own files to the build machine.
		for path in [full_path, *full_path.rglob("*")]:
			if path.is_symlink():
				frappe.throw(frappe._("Symbolic links are not allowed: {0}").format(path))

		for folder in REQUIRED_FOLDERS:
			if not (full_path / folder).is_dir():
				frappe.throw(
					frappe._("The required folder {0} does not exist or is not a directory.").format(
						full_path / folder
					)
				)

		extra = {path.name for path in full_path.iterdir()} - set(REQUIRED_FOLDERS)
		if extra:
			frappe.throw(
				frappe._("The application path {0} contains files or folders not allowed: {1}").format(
					full_path, ", ".join(sorted(extra))
				)
			)

	def validate_packages(self, folder):
		"""Every line of config/packages.txt must be an apt package name."""
		for package in get_packages(folder, self.base_path):
			if not PACKAGE_NAME.match(package):
				frappe.throw(frappe._("{0} is not a valid apt package name.").format(package))

	def validate_script_files(self, folder):
		"""Cargo runs every script over SSH, so the folder holds only shell scripts."""
		scripts_path = self.base_path / folder / "scripts"
		scripts = list(scripts_path.iterdir())
		if not scripts:
			frappe.throw(frappe._("The scripts folder {0} is empty.").format(scripts_path))

		for script in scripts:
			if not script.is_file() or script.suffix != ".sh":
				frappe.throw(frappe._("Only .sh files are allowed in scripts: {0}").format(script))


def get_packages(folder: str, base_path: Path) -> list[str]:
	"""Apt packages from the folder's config/packages.txt, one per line. Blank lines and `#`
	comments are skipped."""
	path = base_path / folder / PACKAGES_FILE
	if not path.is_file():
		return []

	lines = (line.split("#", 1)[0].strip() for line in path.read_text().splitlines())
	return [line for line in lines if line]
