import re

import frappe
import requests

RELEASES_URL = "https://api.github.com/repos/frappe/pilot/releases"
FRAPPE_COMMIT_URL = "https://api.github.com/repos/frappe/frappe/commits/{branch}"
FRAPPE_SOURCE_URL = "https://raw.githubusercontent.com/frappe/frappe/{ref}/frappe/__init__.py"
FRAPPE_VERSION = re.compile(r'^__version__ = "([^"]+)"', re.MULTILINE)
TIMEOUT = 30


def get_frappe_head_commit(branch: str) -> str:
	"""The commit `branch` of frappe/frappe points at now."""
	response = requests.get(
		FRAPPE_COMMIT_URL.format(branch=branch),
		headers={"Accept": "application/vnd.github.sha"},
		timeout=TIMEOUT,
	)
	response.raise_for_status()

	return response.text.strip()


def get_frappe_release(ref: str) -> str:
	"""The Frappe version a bench built from `ref` reports, such as 17.0.0-dev.

	An app release names a range of Frappe versions, so picking one needs the version
	itself, not the branch."""
	response = requests.get(FRAPPE_SOURCE_URL.format(ref=ref), timeout=TIMEOUT)
	response.raise_for_status()

	match = FRAPPE_VERSION.search(response.text)
	if not match:
		frappe.throw(frappe._("Frappe {0} does not declare its version.").format(ref))

	return match.group(1)


def get_latest_pilot_release() -> str:
	"""The newest published Pilot release tag.

	Every Pilot release is a prerelease today, so `releases/latest` answers 404 and
	this reads the release list instead."""
	response = requests.get(RELEASES_URL, params={"per_page": 10}, timeout=TIMEOUT)
	response.raise_for_status()

	for release in response.json():
		if not release.get("draft") and release.get("tag_name"):
			return release["tag_name"]

	frappe.throw(frappe._("Pilot has no published release."))
