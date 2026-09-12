import frappe
import requests

RELEASES_URL = "https://api.github.com/repos/frappe/pilot/releases"
TIMEOUT = 30


def latest_pilot_release() -> str:
	"""The newest published Pilot release tag.

	Every Pilot release is a prerelease today, so `releases/latest` answers 404 and
	this reads the release list instead."""
	response = requests.get(RELEASES_URL, params={"per_page": 10}, timeout=TIMEOUT)
	response.raise_for_status()

	for release in response.json():
		if not release.get("draft") and release.get("tag_name"):
			return release["tag_name"]

	frappe.throw(frappe._("Pilot has no published release."))
