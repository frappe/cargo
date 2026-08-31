import frappe
from frappe import _

CENTRAL_USER = "central@cargo.local"
SERVICE_ROLE = "Cargo Service"


import typing

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings


@frappe.whitelist(methods=["POST"])
def configure(central_url: str, central_access_token: str, atlas_url: str, atlas_access_token: str) -> dict:
	"""Central hands this host everything it needs to reach Atlas and Central."""
	_only_central()

	settings = frappe.get_single("Cargo Settings")
	settings.update(
		{
			"central_url": central_url.rstrip("/"),
			"central_access_token": central_access_token,
			"atlas_url": atlas_url.rstrip("/"),
			"atlas_access_token": atlas_access_token,
		}
	)
	settings.save(ignore_permissions=True)

	return {"configured": True, "site": frappe.local.site}


@frappe.whitelist(methods=["GET"])
def status() -> dict:
	"""What Central checks to know this host is alive and configured."""
	_only_central()
	settings: CargoSettings = frappe.get_single("Cargo Settings")

	return {
		"site": frappe.local.site,
		"configured": bool(
			settings.central_url
			and settings.get_password("central_access_token", raise_exception=False)
			and settings.atlas_url
			and settings.get_password("atlas_access_token", raise_exception=False)
		),
	}


def _only_central() -> None:
	"""These two endpoints are all Central ever reaches, so the role that opens them
	carries nothing else."""
	if SERVICE_ROLE not in frappe.get_roles() and "System Manager" not in frappe.get_roles():
		frappe.throw(_("Not permitted."), frappe.PermissionError)


def issue_api_credentials() -> None:
	"""Print the key pair Central authenticates with. Run once, from setup.sh.

	The user carries only `Cargo Service`, a desk-less role: a leaked pair can configure
	this host and read its status, and nothing else."""
	if not frappe.db.exists("Role", SERVICE_ROLE):
		frappe.get_doc({"doctype": "Role", "role_name": SERVICE_ROLE, "desk_access": 0}).insert(
			ignore_permissions=True
		)

	if not frappe.db.exists("User", CENTRAL_USER):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": CENTRAL_USER,
				"first_name": "Central",
				"user_type": "System User",
				"send_welcome_email": 0,
				"roles": [{"role": SERVICE_ROLE}],
			}
		).insert(ignore_permissions=True)

	user = frappe.get_doc("User", CENTRAL_USER)
	if SERVICE_ROLE not in {row.role for row in (user.get("roles") or [])}:
		user.append("roles", {"role": SERVICE_ROLE})

	api_secret = frappe.generate_hash(length=32)
	if not user.api_key:
		user.api_key = frappe.generate_hash(length=15)
	user.api_secret = api_secret
	user.save(ignore_permissions=True)
	frappe.db.commit()

	print(f"api_key={user.api_key}")
	print(f"api_secret={api_secret}")
