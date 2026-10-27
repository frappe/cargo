import re
import shlex
import typing

import frappe
import yaml
from frappe import _
from frappe.utils import validate_email_address

from cargo.auth import verify_token

if typing.TYPE_CHECKING:
	from cargo.image_builder.doctype.one_click_image.one_click_image import OneClickImage

# Each input becomes a shell variable in inputs.env, so its name must be one.
INPUT_NAME = re.compile(r"[a-z][a-z0-9_]*")
DOMAIN = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True)
@verify_token
def get_supported_one_click_images():
	"""The newest completed image of each application, which is the one a new VM boots from."""
	newest = {}
	for image in frappe.db.get_all(
		"One Click Image",
		{"status": "Completed"},
		["name", "application", "os_version", "information", "snapshot_id"],
		order_by="creation desc",
	):
		newest.setdefault(image.application, image)

	return [
		{
			"application": image.application,
			"title": frappe.get_doc("One Click Image", image.name).application_config["title"],
			"os_version": image.os_version,
			"information": image.information,
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
	config = image.application_config
	return {
		"inputs": config.get("inputs", []),
		"minimum_size": config["minimum_size"],
		"ports": config.get("ports", []),
	}


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True)
@verify_token
def get_one_click_image_user_data(application: str, inputs: dict | str | None = None) -> str:
	"""Cloud-init user data writing the user's answers to /etc/one-click/inputs.env, which the
	application's 001_onboot reads at first boot. Empty when nothing was answered. A blank
	`generate` input is left out, so the VM makes the secret and it never leaves the VM."""
	answers = frappe.parse_json(inputs or {})
	image: OneClickImage = frappe.get_last_doc(
		"One Click Image", {"application": application, "status": "Completed"}
	)
	declared = {item["name"]: item for item in image.application_config.get("inputs", [])}
	if unknown := set(answers) - set(declared):
		frappe.throw(_("{0} has no input {1}.").format(application, ", ".join(sorted(unknown))))

	lines = []
	for name, item in declared.items():
		value = str(answers.get(name) or "").strip()
		label = item.get("label", name)
		if not value:
			if item.get("required"):
				frappe.throw(_("{0} is required.").format(label))
			continue

		kind = item.get("type", "string")
		pattern = item.get("pattern")
		if (
			not INPUT_NAME.fullmatch(name)
			or len(value) > item.get("max_length", 255)
			or any(character in value for character in "\r\n\0")
			or (kind == "domain" and not DOMAIN.fullmatch(value))
			or (kind == "email" and not validate_email_address(value))
			or (kind == "password" and len(value) < 12)
			or (pattern and not re.fullmatch(pattern, value))
		):
			frappe.throw(_("Enter a valid {0}.").format(label))

		lines.append(f"{name.upper()}={shlex.quote(value)}")

	if not lines:
		return ""

	files = [
		{
			"path": "/etc/one-click/inputs.env",
			"owner": "root:root",
			"permissions": "0600",
			"content": "\n".join(lines) + "\n",
		}
	]
	return "#cloud-config\n" + yaml.safe_dump({"write_files": files}, sort_keys=False)
