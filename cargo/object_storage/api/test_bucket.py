# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.exceptions import FrappeTypeError
from frappe.tests import IntegrationTestCase

from cargo.object_storage.api.bucket import (
	actions_for,
	create_bucket,
	delete_bucket,
	rotate_credentials,
	set_quota,
)
from cargo.object_storage.garage.actions import Actions
from cargo.object_storage.garage.models import BucketCredentials
from cargo.testing import use_test_settings

BUCKET = "team-alpha"
CREDENTIALS = BucketCredentials(access_key="GK-access", secret_access_key="shh")


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

	@contextmanager
	def caller(self):
		"""An authenticated caller, and a cluster that answers without a gateway."""
		with (
			patch("cargo.auth.authenticate_request", return_value=frappe._dict(aud="central-admin")),
			patch.object(Actions, "provision_bucket", return_value=CREDENTIALS) as add,
			patch.object(Actions, "remove_bucket") as remove,
			patch.object(Actions, "rotate_credentials", return_value=CREDENTIALS) as rotate,
			patch.object(Actions, "set_quota") as quota,
		):
			yield frappe._dict(add=add, remove=remove, rotate=rotate, quota=quota)

	def test_creating_hands_back_the_key_that_opens_the_bucket(self):
		with self.caller() as garage:
			answer = create_bucket(name=BUCKET, region=self.region)

		garage.add.assert_called_once_with(BUCKET)
		self.assertEqual(answer["name"], BUCKET)
		self.assertEqual(answer["region"], self.region)
		self.assertEqual(answer["credentials"]["secret_access_key"], "shh")

	def test_setting_a_quota_answers_with_the_cap_it_applied(self):
		with self.caller() as garage:
			answer = set_quota(name=BUCKET, size_gib=5, region=self.region)

		garage.quota.assert_called_once_with(BUCKET, 5)
		self.assertEqual(answer, {"name": BUCKET, "region": self.region, "size_gib": 5})

	def test_a_quota_arriving_as_text_is_read_as_a_number(self):
		"""Every HTTP argument arrives as a string."""
		with self.caller() as garage:
			set_quota(name=BUCKET, size_gib="5", region=self.region)

		garage.quota.assert_called_once_with(BUCKET, 5)

	def test_a_quota_of_zero_or_less_is_refused(self):
		for size in ("0", "-1"):
			with self.subTest(size=size), self.caller() as garage:
				with self.assertRaises(frappe.ValidationError):
					set_quota(name=BUCKET, size_gib=size, region=self.region)
				garage.quota.assert_not_called()

	def test_a_quota_that_is_not_a_number_is_refused_by_the_signature(self):
		with self.caller() as garage:
			with self.assertRaises(FrappeTypeError):
				set_quota(name=BUCKET, size_gib="not a number", region=self.region)
			garage.quota.assert_not_called()

	def test_deleting_names_what_went(self):
		with self.caller() as garage:
			answer = delete_bucket(name=BUCKET, region=self.region)

		garage.remove.assert_called_once_with(BUCKET)
		self.assertEqual(answer, {"name": BUCKET, "region": self.region})

	def test_rotating_hands_back_the_key_that_replaces_the_old_one(self):
		with self.caller() as garage:
			answer = rotate_credentials(name=BUCKET, region=self.region)

		garage.rotate.assert_called_once_with(BUCKET)
		self.assertEqual(answer["name"], BUCKET)
		self.assertEqual(answer["credentials"]["access_key"], "GK-access")

	def test_an_unauthenticated_caller_reaches_no_cluster(self):
		with patch.object(Actions, "add_bucket") as add:
			with self.assertRaises(frappe.AuthenticationError):
				create_bucket(name=BUCKET, region=self.region)

		add.assert_not_called()

	def test_a_call_for_another_region_is_refused(self):
		"""One Cargo serves one region: the caller is pointed at the wrong host."""
		with self.assertRaises(frappe.PermissionError):
			actions_for("somewhere-else")

	def test_a_region_with_nothing_serving_answers_no_bucket(self):
		self.cluster.db_set("status", "Failed")

		with self.assertRaises(frappe.ValidationError):
			actions_for(self.region)

	def test_a_critical_cluster_is_not_handed_work(self):
		self.cluster.db_set("health", "Critical")

		with self.assertRaises(frappe.ValidationError):
			actions_for(self.region)
