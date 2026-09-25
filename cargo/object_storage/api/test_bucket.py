# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from contextlib import contextmanager
from unittest.mock import Mock, patch

import frappe
from frappe.exceptions import FrappeTypeError
from frappe.tests import IntegrationTestCase

from cargo.object_storage.api.bucket import (
	add_credentials,
	check_region,
	create_bucket,
	delete_bucket,
	get_usage,
	remove_credentials,
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
NEW_KEY = {"accessKeyId": "GK-new", "secretAccessKey": "fresh", "name": f"{BUCKET}-key"}
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
		# A raw delete leaves the child rows, which a new bucket of the same name would load.
		frappe.db.delete("Bucket Credential", {"parent": BUCKET})

	@contextmanager
	def caller(self):
		"""An authenticated caller, and a Garage that answers without a cluster behind it."""
		garage = Mock()
		garage.create_bucket.return_value = {"id": BUCKET_ID}
		garage.add_bucket_alias.return_value = {}
		garage.bucket.return_value = INFO
		garage.create_key.return_value = KEY
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
		(credential,) = recorded.bucket_credentials
		self.assertEqual(credential.access_key, KEY["accessKeyId"])
		self.assertEqual(credential.get_password("secret_access_key"), KEY["secretAccessKey"])

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

	def recorded_keys(self):
		return [credential.access_key for credential in frappe.get_doc("Bucket", BUCKET).bucket_credentials]

	def test_adding_hands_back_a_new_key_and_keeps_the_first(self):
		with self.caller() as garage:
			self.existing_bucket()
			garage.create_key.return_value = NEW_KEY
			answer = add_credentials(name=BUCKET, region=self.region)

		garage.delete_key.assert_not_called()
		self.assertEqual(answer["name"], BUCKET)
		self.assertEqual(answer["credentials"], {"access_key": "GK-new", "secret_access_key": "fresh"})
		self.assertEqual(self.recorded_keys(), [KEY["accessKeyId"], "GK-new"])

	def test_rotating_hands_back_the_key_that_replaces_the_chosen_one(self):
		with self.caller() as garage:
			self.existing_bucket()
			garage.create_key.return_value = NEW_KEY
			answer = rotate_credentials(name=BUCKET, region=self.region, access_key=KEY["accessKeyId"])

		garage.delete_key.assert_called_once_with(KEY["accessKeyId"])
		self.assertEqual(answer["name"], BUCKET)
		self.assertEqual(answer["credentials"]["access_key"], "GK-new")
		self.assertEqual(self.recorded_keys(), ["GK-new"])

	def test_removing_names_the_key_that_went(self):
		with self.caller() as garage:
			self.existing_bucket()
			garage.create_key.return_value = NEW_KEY
			add_credentials(name=BUCKET, region=self.region)
			garage.bucket.return_value = {**INFO, "keys": [{}, {}]}
			answer = remove_credentials(name=BUCKET, region=self.region, access_key=KEY["accessKeyId"])

		garage.delete_key.assert_called_once_with(KEY["accessKeyId"])
		self.assertEqual(answer, {"name": BUCKET, "region": self.region, "access_key": KEY["accessKeyId"]})
		self.assertEqual(self.recorded_keys(), ["GK-new"])

	def test_removing_the_last_key_is_refused(self):
		with self.caller() as garage:
			self.existing_bucket()
			garage.bucket.return_value = {**INFO, "keys": [{}]}
			with self.assertRaisesRegex(frappe.ValidationError, "at least one key"):
				remove_credentials(name=BUCKET, region=self.region, access_key=KEY["accessKeyId"])

		garage.delete_key.assert_not_called()

	def test_an_unauthenticated_caller_gets_no_key(self):
		with patch.object(Bucket, "issue_credentials") as issue:
			with self.assertRaises(frappe.AuthenticationError):
				add_credentials(name=BUCKET, region=self.region)

		issue.assert_not_called()

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

	def test_a_name_garage_already_holds_leaves_the_winners_bucket_alone(self):
		"""The usual duplicate. Garage refuses the alias in before_insert; add_bucket drops the
		bucket it just made, and the cleanup for a failed insert must not touch anything
		further: the name now answers to the caller that won it."""
		with self.caller() as garage:
			garage.add_bucket_alias.side_effect = Error("AddBucketAlias answered 400: already exists")
			with self.assertRaisesRegex(frappe.ValidationError, "already taken"):
				create_bucket(name=BUCKET, region=self.region)

			garage.delete_bucket.assert_called_once_with(BUCKET_ID)
			garage.delete_key.assert_not_called()
			self.assertFalse(frappe.db.exists("Bucket", BUCKET))

	def test_a_bucket_made_for_an_insert_that_failed_is_taken_back_out(self):
		"""Garage accepted, then the primary key refused: the stores had drifted. The bucket
		and key this call made go with the row that was never written."""
		with self.caller() as garage:
			self.existing_bucket()
			garage.reset_mock()
			with self.assertRaises(frappe.DuplicateEntryError):
				create_bucket(name=BUCKET, region=self.region)

			garage.delete_bucket.assert_called_once_with(BUCKET_ID)
			garage.delete_key.assert_called_once_with(KEY["accessKeyId"])
