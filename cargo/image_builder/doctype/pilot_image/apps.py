import json
from dataclasses import dataclass
from pathlib import Path

import frappe
from frappe import _
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

SIGNUP_APPS = ("erpnext", "crm", ("erpnext", "hrms"), ("telephony", "helpdesk"), "gameplan")


@dataclass(frozen=True)
class AppRelease:
	"""One marketplace release, pinned to the commit the image installs."""

	name: str
	version: str
	repo: str
	commit: str
	requires: tuple[str, ...]


def get_registry_path() -> Path:
	"""Get registry cache path"""
	return Path(frappe.utils.get_bench_path()).parent.parent / "system" / "registry-cache"


def get_compatible_app_commit(app: str, frappe_version: str) -> AppRelease:
	"""Get the newest stable release if stable is not found get the compatible release."""
	registry_path = get_registry_path()

	if not registry_path.exists() or not registry_path.is_dir():
		frappe.throw(_("Registry cache not found."))

	app_registry_file = registry_path / "apps" / f"{app}.json"
	if not app_registry_file.exists():
		frappe.throw(_("App registry file not found for {0}.").format(app))

	app_registry = json.loads(app_registry_file.read_text())
	version = Version(frappe_version)

	compatible = []
	for release in app_registry["releases"]:
		try:
			# A develop Frappe reports a pre-release, which only matches when allowed, as in Pilot.
			if release.get("frappe_core") and version in SpecifierSet(
				release["frappe_core"], prereleases=True
			):
				compatible.append(release)
		except InvalidSpecifier:
			continue

	stable = [release for release in compatible if release.get("channel") != "nightly"]
	if not (stable or compatible):
		frappe.throw(_("{0} has no release for Frappe {1}.").format(app, frappe_version))

	release = max(stable or compatible, key=lambda release: Version(release["version"]))
	return AppRelease(
		name=app,
		version=release["version"],
		# Can fetch from apps.json as well but it's fine since only signup apps
		repo=f"https://github.com/frappe/{app}.git",
		commit=release["commit"],
		requires=tuple(release.get("dependencies") or {}),
	)
