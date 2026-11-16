import frappe
from frappe.installer import install_app


def execute():
	"""Install cloudtoolkit before the doctypes sync. It takes over the Pipeline module of the
	`pipeline` app it was renamed from, so the Pipeline rows stay."""
	if "cloudtoolkit" not in frappe.get_installed_apps():
		install_app("cloudtoolkit")
