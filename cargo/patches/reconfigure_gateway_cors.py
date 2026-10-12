import frappe


def execute():
	"""Route gateways again, so S3 accepts presigned uploads from the Central dashboard."""
	for name in frappe.get_all("Object Storage Cluster", filters={"status": "Active"}, pluck="name"):
		frappe.enqueue_doc("Object Storage Cluster", name, "configure_gateway_routing", queue="long")
