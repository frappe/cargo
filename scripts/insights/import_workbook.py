import sys

import frappe

WORKBOOK_TITLE = "Cargo"


def import_workbook(path: str) -> str:
	"""Update the Cargo workbook in place. Names stay the same, so dashboard URLs keep working."""
	workbook = frappe.get_file_json(path)
	name = frappe.db.get_value("Insights Workbook", {"title": WORKBOOK_TITLE})
	target = frappe.get_doc("Insights Workbook", name) if name else frappe.new_doc("Insights Workbook")
	if not name:
		target.title = WORKBOOK_TITLE
		target.insert()

	# Saving a dashboard queues a headless browser preview job. Skip it.
	frappe.flags.in_patch = True
	target.restore_workbook_contents(workbook, target.name, ignore_permissions=True, keep_names=True)
	frappe.db.commit()  # nosemgrep
	return target.name


if __name__ == "__main__":
	site, path = sys.argv[1:3]
	frappe.init(site)
	frappe.connect()
	try:
		print(f"Updated workbook {import_workbook(path)}.")
	finally:
		frappe.destroy()
