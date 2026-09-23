import frappe
from frappe import _

from cargo.auth import verify_token
from cargo.object_storage.doctype.bucket.bucket import Bucket
from cargo.object_storage.models import (
	BucketCredentials,
	BucketUsageResponse,
	CreateBucketResponse,
	DeleteBucketResponse,
	RotateCredentialsResponse,
	SetQuotaResponse,
)


def check_region(region: str) -> None:
	"""One Cargo serves one region, so a call naming another is refused rather than answered
	from this one."""
	own = frappe.db.get_single_value("Cargo Settings", "region", cache=True)
	if region != own:
		frappe.throw(
			_("This Cargo serves region {0}, not {1}.").format(own or "(none)", region),
			frappe.PermissionError,
		)


def serving_cluster() -> str:
	"""The cluster a new bucket goes on. Named here rather than defaulted on the record, so
	the choice stays visible at the one call that makes it."""
	serving = frappe.get_all(
		"Object Storage Cluster", filters={"status": "Active", "health": ("!=", "Critical")}, pluck="name"
	)
	if not serving:
		frappe.throw(_("No object storage cluster here is serving."))

	# If we have more than one bucket cluster we need to pass the cluster or smarter logic to get the right one.
	if len(serving) > 1:
		frappe.throw(_("This region has more than one serving cluster: {0}.").format(", ".join(serving)))

	return serving[0]


def bucket_for(name: str, region: str) -> Bucket:
	"""The record for a bucket this Cargo keeps, or a not-found for one it does not."""
	check_region(region)

	return frappe.get_doc("Bucket", name)


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True, methods=["POST"])
@verify_token
def create_bucket(name: str, region: str) -> dict:
	"""A bucket and the one key that opens it. The secret is handed back here and nowhere
	else: Cargo keeps no copy a caller can read back."""
	check_region(region)
	bucket: Bucket = frappe.get_doc({"doctype": "Bucket", "bucket_name": name, "cluster": serving_cluster()})
	try:
		bucket.insert(ignore_permissions=True)
	except Exception:
		bucket.discard_provisioned()
		raise

	return CreateBucketResponse(
		name=name,
		region=region,
		credentials=BucketCredentials(
			# Frappe masks the field once it is encrypted, so the secret is read back out.
			access_key=bucket.access_key,
			secret_access_key=bucket.get_password("secret_access_key"),
		),
	).asdict()


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True, methods=["POST"])
@verify_token
def delete_bucket(name: str, region: str) -> dict:
	"""Drop a bucket and its key. Garage refuses a non-empty bucket, so objects are safe."""
	bucket_for(name, region).delete(ignore_permissions=True)

	return DeleteBucketResponse(name=name, region=region).asdict()


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True, methods=["POST"])
@verify_token
def rotate_credentials(name: str, region: str) -> dict:
	"""A new key for this bucket, and the end of the one it replaces. Returned once."""
	bucket = bucket_for(name, region)
	credentials = bucket.rotate_key()
	bucket.save(ignore_permissions=True)

	return RotateCredentialsResponse(name=name, region=region, credentials=credentials).asdict()


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True, methods=["POST"])
@verify_token
def get_usage(name: str, region: str) -> dict:
	"""What this bucket holds, against its caps. A counter read, not a scan."""
	return BucketUsageResponse(name=name, region=region, usage=bucket_for(name, region).get_usage()).asdict()


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True, methods=["POST"])
@verify_token
def set_quota(name: str, size_gib: int, region: str, max_objects: int) -> dict:
	"""This buckets quota in GIB and object count."""
	if size_gib < 0 or max_objects < 0:
		frappe.throw(_("A bucket quota cannot be negative. Zero lifts the cap."))

	bucket = bucket_for(name, region)
	bucket.max_size_gib = size_gib
	bucket.max_objects = max_objects
	bucket.save(ignore_permissions=True)

	return SetQuotaResponse(name=name, region=region, size_gib=size_gib).asdict()
