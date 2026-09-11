import frappe
from frappe import _

from cargo.auth import verify_token
from cargo.object_storage.garage.actions import Actions
from cargo.object_storage.garage.models import (
	CreateBucketResponse,
	DeleteBucketResponse,
	RotateCredentialsResponse,
)


def actions_for(region: str) -> Actions:
	"""The bucket work of the region's serving cluster. One Cargo serves one region, so a
	call naming another is refused rather than answered from this one."""
	own = frappe.db.get_single_value("Cargo Settings", "region", cache=True)
	if region != own:
		frappe.throw(
			_("This Cargo serves region {0}, not {1}.").format(own or "(none)", region),
			frappe.PermissionError,
		)

	serving = frappe.get_all(
		"Object Storage Cluster", filters={"status": "Active", "health": ("!=", "Critical")}, pluck="name"
	)
	if not serving:
		frappe.throw(_("No object storage cluster in {0} is serving.").format(region))

	# Nothing says which one a bucket belongs on, so guessing is worse than refusing.
	if len(serving) > 1:
		frappe.throw(_("{0} has more than one serving cluster: {1}.").format(region, ", ".join(serving)))

	return Actions(frappe.get_doc("Object Storage Cluster", serving[0]))


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True, methods=["POST"])
@verify_token
def create_bucket(name: str, region: str) -> dict:
	"""A bucket and the one key that opens it. The secret is handed back here and nowhere
	else: Cargo keeps no copy."""
	return CreateBucketResponse(
		name=name, region=region, credentials=actions_for(region).provision_bucket(name)
	).asdict()


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True, methods=["POST"])
@verify_token
def delete_bucket(name: str, region: str) -> dict:
	"""Drop a bucket and its key. Garage refuses a non-empty bucket, so objects are safe."""
	actions_for(region).remove_bucket(name)

	return DeleteBucketResponse(name=name, region=region).asdict()


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True, methods=["POST"])
@verify_token
def rotate_credentials(name: str, region: str) -> dict:
	"""A new key for this bucket, and the end of the one it replaces. Returned once."""
	return RotateCredentialsResponse(
		name=name, region=region, credentials=actions_for(region).rotate_credentials(name)
	).asdict()
