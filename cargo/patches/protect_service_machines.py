import frappe

PROTECTED_OWNERS = ("Object Storage Cluster", "Datum Server")


def execute():
	"""Protect the Garage and Datum machines built before Cargo asked Atlas to. One Atlas refusal is
	logged, not raised, so the rest are still protected and the migrate goes on."""
	names = frappe.get_all(
		"Machine",
		filters={
			"reference_doctype": ("in", PROTECTED_OWNERS),
			"status": ("not in", ("Draft", "Terminated")),
			"vm_id": ("is", "set"),
			"is_termination_protected": 0,
		},
		pluck="name",
	)
	for name in names:
		try:
			frappe.get_doc("Machine", name).set_termination_protection(True)
		except Exception:
			frappe.log_error(title=f"Could not protect machine {name} from termination")
