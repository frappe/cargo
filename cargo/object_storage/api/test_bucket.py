# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from contextlib import contextmanager
from unittest.mock import Mock, patch

import frappe
from frappe.exceptions import FrappeTypeError
from frappe.tests import IntegrationTestCase

from cargo.object_storage.api.bucket import (
	check_region,
	create_bucket,
	delete_bucket,
	get_usage,
	rotate_credentials,
	serving_cluster,
	set_quota,
)
from cargo.object_storage.client import Error
from cargo.object_storage.doctype.bucket.bucket import Bucket
from cargo.testing import use_test_settings

BUCKET = "team-alpha"
BUCKET_ID = "b1"
KEY = {"accessKeyId": "GK-access", "secretAccessKey": "shh", "name": f"{BUCKET}-key"}
INFO = {
	"id": BUCKET_ID,
	"bytes": 20971520,
	"objects": 4,
	"quotas": {"maxSize": 1073741824, "maxObjects": 100},
}


class IntegrationTestBucketApi(IntegrationTestCase):
	"""The calls Central and Atlas make, and what Cargo will answer them with."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		self.region = frappe.db.get_single_value("Cargo Settings", "region")
		# This site may already hold clusters; the region is meant to have one that serves.
		for name in frappe.get_all("Object Storage Cluster", pluck="name"):
			frappe.db.set_value("Object Storage Cluster", name, "status", "Draft")

		self.cluster = frappe.get_doc({"doctype": "Object Storage Cluster"}).insert()
		self.cluster.db_set({"status": "Active", "health": "Healthy"})
		frappe.db.delete("Bucket", {"bucket_name": BUCKET})

	@contextmanager
	def caller(self):
		"""An authenticated caller, and a Garage that answers without a cluster behind it."""
		garage = Mock()
		garage.create_bucket.return_value = {"id": BUCKET_ID}
		garage.add_bucket_alias.return_value = {}
		garage.bucket.return_value = INFO
		garage.create_key.return_value = KEY
		garage.key.return_value = KEY
		garage.allow_bucket_key.return_value = {}

		with (
			patch("cargo.auth.authenticate_request", return_value=frappe._dict(aud="central-admin")),
			patch.object(Bucket, "garage", garage),
		):
			yield garage

	def existing_bucket(self):
		return frappe.get_doc(
			{"doctype": "Bucket", "bucket_name": BUCKET, "cluster": self.cluster.name}
		).insert()

	def test_creating_hands_back_the_key_that_opens_the_bucket(self):
		with self.caller() as garage:
			answer = create_bucket(name=BUCKET, region=self.region)

		garage.add_bucket_alias.assert_called_once_with(BUCKET_ID, BUCKET)
		self.assertEqual(answer["name"], BUCKET)
		self.assertEqual(answer["region"], self.region)
		self.assertEqual(answer["credentials"]["secret_access_key"], "shh")

	def test_a_created_bucket_is_recorded_with_its_key(self):
		"""The record is what a later call reads: nothing asks Garage for the name again."""
		with self.caller():
			create_bucket(name=BUCKET, region=self.region)

		recorded = frappe.get_doc("Bucket", BUCKET)
		self.assertEqual(recorded.cluster, self.cluster.name)
		self.assertEqual(recorded.access_key, KEY["accessKeyId"])
		self.assertEqual(recorded.get_password("secret_access_key"), KEY["secretAccessKey"])

	def test_setting_a_quota_answers_with_the_cap_it_applied(self):
		with self.caller() as garage:
			self.existing_bucket()
			answer = set_quota(name=BUCKET, size_gib=5, region=self.region, max_objects=0)

		garage.set_bucket_quota.assert_called_once_with(BUCKET_ID, 5 * 1024**3, None)
		self.assertEqual(answer, {"name": BUCKET, "region": self.region, "size_gib": 5})

	def test_a_quota_arriving_as_text_is_read_as_a_number(self):
		"""Every HTTP argument arrives as a string."""
		with self.caller() as garage:
			self.existing_bucket()
			set_quota(name=BUCKET, size_gib="5", region=self.region, max_objects="0")

		garage.set_bucket_quota.assert_called_once_with(BUCKET_ID, 5 * 1024**3, None)

	def test_a_negative_quota_is_refused(self):
		for size, objects in ((-1, 0), (5, -1)):
			with self.subTest(size=size, objects=objects), self.caller() as garage:
				with self.assertRaisesRegex(frappe.ValidationError, "cannot be negative"):
					set_quota(name=BUCKET, size_gib=size, region=self.region, max_objects=objects)
				garage.set_bucket_quota.assert_not_called()

	def test_a_quota_of_zero_lifts_the_cap(self):
		"""The field says zero is uncapped and Garage lifts a cap with a null, so zero has to
		reach it rather than be refused as out of range."""
		with self.caller() as garage:
			self.existing_bucket().db_set("max_size_gib", 5)
			set_quota(name=BUCKET, size_gib=0, region=self.region, max_objects=0)

		garage.set_bucket_quota.assert_called_once_with(BUCKET_ID, None, None)

	def test_a_quota_that_is_not_a_number_is_refused_by_the_signature(self):
		with self.caller() as garage:
			with self.assertRaises(FrappeTypeError):
				set_quota(name=BUCKET, size_gib="not a number", region=self.region, max_objects=0)
			garage.set_bucket_quota.assert_not_called()

	def test_usage_answers_with_what_is_held_and_the_caps_on_it(self):
		"""Usage and caps in one answer, so a caller needs no second call to work out how
		much of its quota is gone."""
		with self.caller():
			self.existing_bucket()
			answer = get_usage(name=BUCKET, region=self.region)

		self.assertEqual(
			answer,
			{
				"name": BUCKET,
				"region": self.region,
				"usage": {
					"used_bytes": 20971520,
					"object_count": 4,
					"quota_bytes": 1073741824,
					"quota_objects": 100,
				},
			},
		)

	def test_deleting_names_what_went(self):
		with self.caller() as garage:
			self.existing_bucket()
			answer = delete_bucket(name=BUCKET, region=self.region)

		garage.delete_bucket.assert_called_once_with(BUCKET_ID)
		self.assertEqual(answer, {"name": BUCKET, "region": self.region})
		self.assertFalse(frappe.db.exists("Bucket", BUCKET))

	def test_working_a_bucket_this_cargo_never_made_is_a_not_found(self):
		with self.caller(), self.assertRaises(frappe.DoesNotExistError):
			get_usage(name="never-made", region=self.region)

	def test_rotating_hands_back_the_key_that_replaces_the_old_one(self):
		with self.caller() as garage:
			self.existing_bucket()
			answer = rotate_credentials(name=BUCKET, region=self.region)

		garage.delete_key.assert_called_once_with(KEY["accessKeyId"])
		self.assertEqual(answer["name"], BUCKET)
		self.assertEqual(answer["credentials"]["access_key"], "GK-access")

	def test_an_unauthenticated_caller_reaches_no_cluster(self):
		with patch.object(Bucket, "add_bucket") as add:
			with self.assertRaises(frappe.AuthenticationError):
				create_bucket(name=BUCKET, region=self.region)

		add.assert_not_called()

	def test_a_call_for_another_region_is_refused(self):
		"""One Cargo serves one region: the caller is pointed at the wrong host."""
		with self.assertRaises(frappe.PermissionError):
			check_region("somewhere-else")

	def test_a_region_with_nothing_serving_answers_no_bucket(self):
		self.cluster.db_set("status", "Failed")

		with self.assertRaises(frappe.ValidationError):
			serving_cluster()

	def test_a_critical_cluster_is_not_handed_work(self):
		self.cluster.db_set("health", "Critical")

		with self.assertRaises(frappe.ValidationError):
			serving_cluster()

	def test_a_bucket_must_name_the_cluster_it_lives_on(self):
		"""Nothing guesses it on the record: a Cargo serving two clusters would silently put
		the bucket on whichever one the query returned first."""
		with self.caller(), self.assertRaises(frappe.MandatoryError):
			frappe.get_doc({"doctype": "Bucket", "bucket_name": BUCKET}).insert(ignore_permissions=True)

	def test_a_name_garage_already_holds_never_reaches_the_record(self):
		"""The usual duplicate. Garage's cluster-global alias answers in before_save, so the
		primary key is never tried and the bucket it just made is taken back out."""
		with self.caller() as garage:
			garage.add_bucket_alias.side_effect = Error("AddBucketAlias answered 400: already exists")
			with self.assertRaisesRegex(frappe.ValidationError, "already taken"):
				create_bucket(name=BUCKET, region=self.region)

			garage.delete_bucket.assert_called_once_with(BUCKET_ID)
			self.assertFalse(frappe.db.exists("Bucket", BUCKET))

	def test_the_primary_key_backstops_a_name_already_recorded(self):
		"""Only reachable once the two stores have drifted: Garage accepts a name this Cargo
		already holds a row for. Nothing translates DuplicateEntryError, which carries no HTTP
		status, so Central reads it as a server error."""
		with self.caller():
			self.existing_bucket()
			with self.assertRaises(frappe.DuplicateEntryError):
				create_bucket(name=BUCKET, region=self.region)
