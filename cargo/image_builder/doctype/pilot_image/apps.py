import json
from dataclasses import dataclass
from pathlib import Path

import frappe
import requests
from frappe import _
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version


@dataclass(frozen=True)
class AppRelease:
	"""One marketplace release, pinned to the commit the image installs."""

	name: str
	version: str
	repo: str
	commit: str
	branch: str
	requires: tuple[str, ...]


def get_registry_path() -> Path:
	"""Pilot keeps the marketplace registry here and refreshes it on its own."""
	return Path(frappe.utils.get_bench_path()).parent.parent / "system" / "registry-cache"


def do_assets_exist(release: AppRelease) -> bool:
	"""Whether the app's CI published prebuilt assets for the release, where Pilot downloads them."""
	tag = f"assets-{release.branch.replace('/', '-')}"
	url = f"{release.repo.removesuffix('.git')}/releases/download/{tag}/{release.name}-{release.commit}.tar.gz.sha256"
	return requests.head(url, allow_redirects=True, timeout=10).ok


def get_registry_releases(app: str) -> list[dict]:
	app_registry_file = get_registry_path() / "apps" / f"{app}.json"
	if not app_registry_file.exists():
		frappe.throw(_("App registry file not found for {0}.").format(app))

	return json.loads(app_registry_file.read_text())["releases"]


def to_app_release(app: str, release: dict) -> AppRelease:
	return AppRelease(
		name=app,
		version=release["version"],
		# Every signup app lives under frappe on GitHub.
		repo=f"https://github.com/frappe/{app}.git",
		commit=release["commit"],
		branch=release["branch"],
		requires=tuple(release.get("dependencies") or {}),
	)


def is_compatible(release: dict, frappe_version: Version) -> bool:
	# A develop Frappe reports a pre-release, which only matches when allowed, as in Pilot.
	try:
		return bool(release.get("frappe_core")) and frappe_version in SpecifierSet(
			release["frappe_core"], prereleases=True
		)
	except InvalidSpecifier:
		return False


def get_latest_stable_release(app: str, frappe_version: str) -> AppRelease | None:
	"""The newest stable release of `app` that runs on `frappe_version`, if it has one."""
	version = Version(frappe_version)
	stable = [
		release
		for release in get_registry_releases(app)
		if release.get("channel") != "nightly" and is_compatible(release, version)
	]
	if not stable:
		return None

	return to_app_release(app, max(stable, key=lambda release: Version(release["version"])))


def get_release_at(app: str, commit: str) -> AppRelease:
	"""The release of `app` made from `commit`."""
	for release in get_registry_releases(app):
		if release["commit"] == commit:
			return to_app_release(app, release)

	frappe.throw(_("{0} has no release at commit {1}.").format(app, commit))


def get_signup_apps() -> list[str]:
	"""The signup apps listed in Cargo Settings."""
	return frappe.get_all(
		"Signup App",
		filters={"parenttype": "Cargo Settings", "parentfield": "signup_apps"},
		pluck="app",
		order_by="idx asc",
	)


def get_install_order(release: AppRelease, frappe_version: str) -> list[AppRelease]:
	"""`release` and every app it requires, each after what it requires. A requirement takes
	its newest stable release."""
	install_order = []
	for requirement in release.requires:
		requirement_release = get_latest_stable_release(requirement, frappe_version)
		if not requirement_release:
			frappe.throw(
				_("{0} requires {1}, which has no stable release for Frappe {2}.").format(
					release.name, requirement, frappe_version
				)
			)

		for required in get_install_order(requirement_release, frappe_version):
			if required not in install_order:
				install_order.append(required)

	install_order.append(release)
	return install_order
