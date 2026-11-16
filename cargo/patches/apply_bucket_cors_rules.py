import frappe


def execute():
	"""Write CORS rules for buckets made before Garage answered CORS, so the dashboard keeps
	working once setup gives the gateway the Caddyfile that leaves CORS to Garage. One refusal
	is logged, not raised, so the rest are still written and the migrate goes on."""
	clusters = frappe.get_all("Object Storage Cluster", filters={"status": "Active"}, pluck="name")
	for name in frappe.get_all("Bucket", filters={"cluster": ("in", clusters)}, pluck="name"):
		try:
			frappe.get_doc("Bucket", name).apply_cors_rules()
		except Exception:
			frappe.log_error(title=f"Could not apply CORS rules to bucket {name}")
