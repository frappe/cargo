import re
import shlex
import typing

import frappe
import yaml
from frappe import _
from frappe.utils import validate_email_address
from frappe.utils.password_strength import test_password_strength

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
		["application", "os_version", "information", "snapshot_id", "app_config"],
		order_by="creation desc",
	):
		newest.setdefault(image.application, image)

	return [
		{
			"application": image.application,
			"title": frappe.parse_json(image.app_config)["title"],
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
	config = image.app
	return {
		"inputs": config.get("inputs", []),
		"minimum_size": config["minimum_size"],
		"ports": config.get("ports", []),
	}


def validate_password(label: str, value: str) -> None:
	"""Refuse a password that zxcvbn scores below 3 of 4, with its advice on how to fix it."""
	result = test_password_strength(value)
	if result["score"] < 3:
		feedback = result["feedback"]
		advice = " ".join([feedback.get("warning") or "", *feedback.get("suggestions", [])]).strip()
		frappe.throw(
			_("{0} is too easy to guess. {1}").format(
				label, advice or _("Use a longer password made of a few uncommon words.")
			)
		)


def validate_answer(name: str, item: dict, value: str) -> None:
	"""Throw a message that says what is wrong with one answer and what the input takes."""
	label = item.get("label", name)
	kind = item.get("type", "string")
	max_length = item.get("max_length", 255)

	if not INPUT_NAME.fullmatch(name):
		frappe.throw(_("The app input {0} is not a valid shell variable name.").format(name))
	if len(value) > max_length:
		frappe.throw(_("{0} must be at most {1} characters.").format(label, max_length))
	if not value.isprintable():
		frappe.throw(
			_("{0} can contain only printable characters, with no line breaks or tabs.").format(label)
		)
	if kind == "email":
		validate_email_address(value, throw=True)
	elif kind == "domain" and not DOMAIN.fullmatch(value):
		frappe.throw(_("{0} must be a domain name in lowercase, such as app.example.com.").format(label))
	elif kind == "password":
		validate_password(label, value)
	if (pattern := item.get("pattern")) and not re.fullmatch(pattern, value):
		frappe.throw(
			_("{0} is not in the expected format. {1}").format(label, item.get("description", "")).strip()
		)


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
	declared = {item["name"]: item for item in image.app.get("inputs", [])}
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

		validate_answer(name, item, value)
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
