import typing

import frappe
from frappe import _

from cargo.auth import verify_token

if typing.TYPE_CHECKING:
	from cargo.image_builder.doctype.one_click_image.one_click_image import OneClickImage


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True)
@verify_token
def get_supported_one_click_images():
	"""The newest completed image of each application, which is the one a new VM boots from."""
	newest = {}
	for image in frappe.db.get_all(
		"One Click Image",
		{"status": "Completed"},
		["application", "os_version", "information", "snapshot_id", "app_config", "logo"],
		order_by="creation desc",
	):
		newest.setdefault(image.application, image)

	return [
		{
			"application": image.application,
			"title": frappe.parse_json(image.app_config)["title"],
			"os_version": image.os_version,
			"information": image.information,
			"logo": image.logo,
			"snapshot_id": image.snapshot_id,
		}
		for image in newest.values()
	]


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True)
@verify_token
def get_one_click_image_requirements(application: str):
	"""What Central needs to launch the application's newest image: the inputs to ask the user
	for, the smallest machine it runs on and the ports to open."""
	image: OneClickImage = frappe.get_last_doc(
		"One Click Image", {"application": application, "status": "Completed"}
	)
	config = image.app
	return {
		"inputs": config.get("inputs", []),
		"minimum_size": config["minimum_size"],
		"ports": config.get("ports", []),
	}
