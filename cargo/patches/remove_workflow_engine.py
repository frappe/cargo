import frappe


def execute():
	"""Drop what the vendored engine left behind once the Pipeline app replaced it. Old run
	history stays in its `tabPress Workflow*` tables."""
	frappe.delete_doc_if_exists("Number Card", "Failed Workflows", force=True)
	frappe.delete_doc_if_exists("Module Def", "Workflow Engine", force=True)
