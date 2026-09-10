# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from cargo.object_storage.garage.actions import Actions
from cargo.object_storage.garage.client import Client, Error

BUCKET = "team-alpha"
BUCKET_ID = "b1"
KEY = {"accessKeyId": "GK-access", "secretAccessKey": "shh", "name": f"{BUCKET}-key"}


class UnitTestBucketActions(UnitTestCase):
	"""What Central and Atlas get when they ask Cargo to work a bucket."""

	def setUp(self):
		self.actions = Actions(frappe._dict(doctype="Object Storage Cluster", name="OSC-0001"))

	def answers(self, **endpoints):
		"""Garage answering each named endpoint, and nothing else reaching the network."""
		patches = [patch.object(Client, name, **answer) for name, answer in endpoints.items()]
		for patcher in patches:
			self.addCleanup(patcher.stop)

		return [patcher.start() for patcher in patches]

	def test_a_bucket_is_named_only_once_it_exists(self):
		"""An abandoned bucket squats no name, so the alias goes on last."""
		create, alias = self.answers(
			create_bucket={"return_value": {"id": BUCKET_ID}},
			add_bucket_alias={"return_value": {}},
		)

		self.assertEqual(self.actions.add_bucket(BUCKET), BUCKET_ID)
		self.assertEqual(create.call_args.args, ())
		self.assertEqual(alias.call_args.args, (BUCKET_ID, BUCKET))

	def test_a_name_someone_else_won_is_answered_not_relayed(self):
		_, _, delete = self.answers(
			create_bucket={"return_value": {"id": BUCKET_ID}},
			add_bucket_alias={"side_effect": Error("AddBucketAlias answered 400: already exists")},
			delete_bucket={"return_value": None},
		)

		with self.assertRaises(frappe.ValidationError) as raised:
			self.actions.add_bucket(BUCKET)

		self.assertIn("already taken", str(raised.exception))
		# The bucket it just made is dropped: nothing names it, so nothing can reach it.
		delete.assert_called_once_with(BUCKET_ID)

	def test_credentials_are_scoped_to_the_bucket_that_was_asked_for(self):
		_, _, allow = self.answers(
			bucket={"return_value": {"id": BUCKET_ID}},
			create_key={"return_value": KEY},
			allow_bucket_key={"return_value": {}},
		)

		credentials = self.actions.issue_credentials(BUCKET)

		self.assertEqual(credentials.access_key, KEY["accessKeyId"])
		self.assertEqual(credentials.secret_access_key, KEY["secretAccessKey"])
		allow.assert_called_once_with(BUCKET_ID, KEY["accessKeyId"])

	def test_a_key_that_cannot_be_granted_is_not_left_live(self):
		_, _, _, delete_key = self.answers(
			bucket={"return_value": {"id": BUCKET_ID}},
			create_key={"return_value": KEY},
			allow_bucket_key={"side_effect": Error("AllowBucketKey answered 500: nope")},
			delete_key={"return_value": None},
		)

		with self.assertRaises(Error):
			self.actions.issue_credentials(BUCKET)

		delete_key.assert_called_once_with(KEY["accessKeyId"])

	def test_a_bucket_that_does_not_exist_gets_no_key(self):
		(create_key,) = self.answers(create_key={"return_value": KEY})
		with patch.object(Client, "bucket", return_value=None):
			with self.assertRaises(frappe.ValidationError):
				self.actions.issue_credentials(BUCKET)

		create_key.assert_not_called()

	def test_removing_a_bucket_takes_its_key_with_it(self):
		_, _, delete_key, delete_bucket = self.answers(
			bucket={"return_value": {"id": BUCKET_ID}},
			key={"return_value": KEY},
			delete_key={"return_value": None},
			delete_bucket={"return_value": None},
		)

		self.actions.remove_bucket(BUCKET)

		delete_key.assert_called_once_with(KEY["accessKeyId"])
		delete_bucket.assert_called_once_with(BUCKET_ID)

	def test_removing_a_bucket_that_is_already_gone_asks_nothing_further(self):
		"""A delete that arrives twice is not a failure."""
		_, delete_bucket = self.answers(bucket={"return_value": None}, delete_bucket={"return_value": None})

		self.actions.remove_bucket(BUCKET)

		delete_bucket.assert_not_called()

	def test_revoking_leaves_the_bucket_alone(self):
		_, delete_key, delete_bucket = self.answers(
			key={"return_value": KEY},
			delete_key={"return_value": None},
			delete_bucket={"return_value": None},
		)

		self.actions.revoke_credentials(BUCKET)

		delete_key.assert_called_once_with(KEY["accessKeyId"])
		delete_bucket.assert_not_called()

	def test_rotating_mints_the_new_key_before_dropping_the_old(self):
		"""A rotation that fails halfway leaves the bucket reachable, not shut."""
		order = []
		old = {"accessKeyId": "GK-old", "secretAccessKey": "was", "name": f"{BUCKET}-key"}
		_, _, _, allow, delete_key = self.answers(
			bucket={"return_value": {"id": BUCKET_ID}},
			key={"return_value": old},
			create_key={"return_value": KEY, "side_effect": lambda name: order.append("create") or KEY},
			allow_bucket_key={"return_value": {}},
			delete_key={"side_effect": lambda access_key_id: order.append("delete")},
		)

		credentials = self.actions.rotate_credentials(BUCKET)

		self.assertEqual(credentials.access_key, KEY["accessKeyId"])
		self.assertEqual(order, ["create", "delete"])
		delete_key.assert_called_once_with(old["accessKeyId"])
		allow.assert_called_once_with(BUCKET_ID, KEY["accessKeyId"])

	def test_rotating_a_bucket_with_no_key_yet_just_issues_one(self):
		_, _, _, _, delete_key = self.answers(
			bucket={"return_value": {"id": BUCKET_ID}},
			key={"return_value": None},
			create_key={"return_value": KEY},
			allow_bucket_key={"return_value": {}},
			delete_key={"return_value": None},
		)

		self.assertEqual(self.actions.rotate_credentials(BUCKET).access_key, KEY["accessKeyId"])
		delete_key.assert_not_called()

	def test_a_bucket_no_key_could_be_made_for_is_taken_back_out(self):
		"""Nobody was handed a way in, so leaving it would leave something unreachable."""
		_, _, _, create_key, unname, delete_bucket = self.answers(
			create_bucket={"return_value": {"id": BUCKET_ID}},
			add_bucket_alias={"return_value": {}},
			bucket={"return_value": {"id": BUCKET_ID}},
			create_key={"side_effect": Error("CreateKey answered 500: nope")},
			remove_bucket_alias={"return_value": {}},
			delete_bucket={"return_value": None},
		)

		with self.assertRaises(Error):
			self.actions.provision_bucket(BUCKET)

		create_key.assert_called_once()
		unname.assert_called_once_with(BUCKET_ID, BUCKET)
		delete_bucket.assert_called_once_with(BUCKET_ID)

	def test_a_provisioned_bucket_answers_with_its_key(self):
		_, _, _, _, _, delete_bucket = self.answers(
			create_bucket={"return_value": {"id": BUCKET_ID}},
			add_bucket_alias={"return_value": {}},
			bucket={"return_value": {"id": BUCKET_ID}},
			create_key={"return_value": KEY},
			allow_bucket_key={"return_value": {}},
			delete_bucket={"return_value": None},
		)

		credentials = self.actions.provision_bucket(BUCKET)

		self.assertEqual(credentials.access_key, KEY["accessKeyId"])
		delete_bucket.assert_not_called()

	def test_the_key_is_put_on_the_bucket_that_was_just_made(self):
		"""Not the one the name points at now: a delete landing in between must not move it."""
		_, _, lookup, _, allow, _ = self.answers(
			create_bucket={"return_value": {"id": BUCKET_ID}},
			add_bucket_alias={"return_value": {}},
			bucket={"return_value": {"id": "some-other-bucket"}},
			create_key={"return_value": KEY},
			allow_bucket_key={"return_value": {}},
			delete_bucket={"return_value": None},
		)

		self.actions.provision_bucket(BUCKET)

		allow.assert_called_once_with(BUCKET_ID, KEY["accessKeyId"])
		lookup.assert_not_called()

	def test_every_bucket_operation_holds_that_bucket_name(self):
		"""Garage arbitrates the name, not the key behind it, so Cargo serialises the rest."""
		self.answers(
			bucket={"return_value": None},
			key={"return_value": None},
			delete_key={"return_value": None},
		)
		with patch.object(Actions, "bucket_lock") as lock:
			self.actions.remove_bucket(BUCKET)
			self.actions.revoke_credentials(BUCKET)

		self.assertEqual([call.args for call in lock.call_args_list], [(BUCKET,), (BUCKET,)])
