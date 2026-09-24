# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from cargo.object_storage.client import Client, Error
from cargo.object_storage.doctype.bucket.bucket import Bucket

BUCKET = "team-alpha"
BUCKET_ID = "b1"
KEY = {"accessKeyId": "GK-access", "secretAccessKey": "shh", "name": f"{BUCKET}-key"}


class UnitTestBucket(UnitTestCase):
	"""What Central and Atlas get when they ask Cargo to work a bucket."""

	def setUp(self):
		self.bucket = Bucket({"doctype": "Bucket", "bucket_name": BUCKET, "cluster": "OSC-0001"})
		self.bucket.name = BUCKET
		self.patch(Bucket, "garage", Client(frappe._dict(doctype="Object Storage Cluster", name="OSC-0001")))
		self.saved = self.patch(Bucket, "save")

	def patch(self, target, attribute, *args, **kwargs):
		patcher = patch.object(target, attribute, *args, **kwargs)
		self.addCleanup(patcher.stop)

		return patcher.start()

	def answers(self, **endpoints):
		"""Garage answering each named endpoint, and nothing else reaching the network."""
		return [self.patch(Client, name, **answer) for name, answer in endpoints.items()]

	def test_a_bucket_is_named_only_once_it_exists(self):
		"""An abandoned bucket squats no name, so the alias goes on last."""
		create, alias = self.answers(
			create_bucket={"return_value": {"id": BUCKET_ID}},
			add_bucket_alias={"return_value": {}},
		)

		self.assertEqual(self.bucket.add_bucket(), BUCKET_ID)
		self.assertEqual(create.call_args.args, ())
		self.assertEqual(alias.call_args.args, (BUCKET_ID, BUCKET))

	def test_a_name_someone_else_won_is_answered_not_relayed(self):
		_, _, delete = self.answers(
			create_bucket={"return_value": {"id": BUCKET_ID}},
			add_bucket_alias={"side_effect": Error("AddBucketAlias answered 400: already exists")},
			delete_bucket={"return_value": None},
		)

		with self.assertRaises(frappe.ValidationError) as raised:
			self.bucket.add_bucket()

		self.assertIn("already taken", str(raised.exception))
		# The bucket it just made is dropped: nothing names it, so nothing can reach it.
		delete.assert_called_once_with(BUCKET_ID)

	def test_a_name_garage_will_not_accept_is_answered_not_relayed(self):
		"""Garage judges the name. Relaying its 400 as a 500 would tell the caller to wait
		for something that will never change."""
		_, _, delete = self.answers(
			create_bucket={"return_value": {"id": BUCKET_ID}},
			add_bucket_alias={
				"side_effect": Error(
					'AddBucketAlias answered 400: {"code": "InvalidBucketName",'
					' "message": "Invalid bucket name: Team-Alpha"}'
				)
			},
			delete_bucket={"return_value": None},
		)
		self.bucket.bucket_name = "Team-Alpha"

		with self.assertRaises(frappe.ValidationError) as raised:
			self.bucket.add_bucket()

		self.assertIn("not a valid bucket name", str(raised.exception))
		delete.assert_called_once_with(BUCKET_ID)

	def test_credentials_are_scoped_to_the_bucket_that_was_asked_for(self):
		_, _, allow = self.answers(
			bucket={"return_value": {"id": BUCKET_ID}},
			create_key={"return_value": KEY},
			allow_bucket_key={"return_value": {}},
		)

		credentials = self.bucket.issue_credentials()

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
			self.bucket.issue_credentials()

		delete_key.assert_called_once_with(KEY["accessKeyId"])

	def test_a_bucket_that_does_not_exist_gets_no_key(self):
		(create_key,) = self.answers(create_key={"return_value": KEY})
		with patch.object(Client, "bucket", return_value=None):
			with self.assertRaises(frappe.ValidationError):
				self.bucket.issue_credentials()

		create_key.assert_not_called()

	def test_usage_is_read_from_the_counters_garage_already_keeps(self):
		"""One metadata read: no listing, and nothing that grows with the object count."""
		(info,) = self.answers(
			bucket={
				"return_value": {
					"id": BUCKET_ID,
					"bytes": 20971520,
					"objects": 4,
					"quotas": {"maxSize": 1073741824, "maxObjects": 100},
				}
			},
		)

		usage = self.bucket.get_usage()

		self.assertEqual(usage.used_bytes, 20971520)
		self.assertEqual(usage.object_count, 4)
		self.assertEqual(usage.quota_bytes, 1073741824)
		self.assertEqual(usage.quota_objects, 100)
		info.assert_called_once_with(BUCKET)

	def test_an_uncapped_bucket_reports_no_quota(self):
		self.answers(bucket={"return_value": {"id": BUCKET_ID, "bytes": 0, "objects": 0, "quotas": None}})

		usage = self.bucket.get_usage()

		self.assertIsNone(usage.quota_bytes)
		self.assertIsNone(usage.quota_objects)

	def test_usage_of_a_bucket_that_does_not_exist_is_refused(self):
		self.answers(bucket={"return_value": None})

		with self.assertRaises(frappe.ValidationError):
			self.bucket.get_usage()

	def test_a_quota_is_sent_to_garage_in_bytes(self):
		"""The form takes GiB; Garage counts bytes, so the conversion happens here."""
		_, quota = self.answers(
			bucket={"return_value": {"id": BUCKET_ID}},
			set_bucket_quota={"return_value": None},
		)
		self.bucket.max_size_gib = 2

		self.bucket.apply_quota()

		quota.assert_called_once_with(BUCKET_ID, 2 * 1024**3, None)

	def test_a_zero_cap_is_sent_as_no_cap(self):
		"""Zero is how the form says uncapped. Sent as a zero it would stop every write."""
		_, quota = self.answers(
			bucket={"return_value": {"id": BUCKET_ID}},
			set_bucket_quota={"return_value": None},
		)
		self.bucket.max_size_gib = 0
		self.bucket.max_objects = 5

		self.bucket.apply_quota()

		quota.assert_called_once_with(BUCKET_ID, None, 5)

	def test_a_quota_on_a_bucket_that_does_not_exist_is_refused(self):
		_, quota = self.answers(
			bucket={"return_value": None},
			set_bucket_quota={"return_value": None},
		)
		self.bucket.max_size_gib = 2

		with self.assertRaises(frappe.ValidationError):
			self.bucket.apply_quota()

		quota.assert_not_called()

	def test_removing_a_bucket_takes_its_key_with_it(self):
		order = []
		_, _, delete_key, delete_bucket = self.answers(
			bucket={"return_value": {"id": BUCKET_ID}},
			key={"return_value": KEY},
			delete_key={"side_effect": lambda access_key_id: order.append("key")},
			delete_bucket={"side_effect": lambda bucket_id: order.append("bucket")},
		)

		self.bucket.on_trash()

		delete_key.assert_called_once_with(KEY["accessKeyId"])
		delete_bucket.assert_called_once_with(BUCKET_ID)
		# The bucket first: a 409 must not leave it live with its key already revoked.
		self.assertEqual(order, ["bucket", "key"])

	def test_a_bucket_whose_delete_is_refused_keeps_its_key(self):
		"""Garage answers 409 while objects remain, so the bucket stays reachable."""
		_, _, delete_key, _ = self.answers(
			bucket={"return_value": {"id": BUCKET_ID}},
			key={"return_value": KEY},
			delete_key={"return_value": None},
			delete_bucket={"side_effect": Error("DeleteBucket answered 409: BucketNotEmpty")},
		)

		with self.assertRaisesRegex(frappe.ValidationError, "still holds objects"):
			self.bucket.on_trash()

		delete_key.assert_not_called()

	def test_a_delete_garage_fails_for_another_reason_is_relayed(self):
		_, _, delete_key, _ = self.answers(
			bucket={"return_value": {"id": BUCKET_ID}},
			key={"return_value": KEY},
			delete_key={"return_value": None},
			delete_bucket={"side_effect": Error("DeleteBucket answered 500: internal")},
		)

		with self.assertRaises(Error):
			self.bucket.on_trash()

		delete_key.assert_not_called()

	def test_removing_a_bucket_that_is_already_gone_asks_nothing_further(self):
		"""A delete that arrives twice is not a failure."""
		_, delete_bucket = self.answers(bucket={"return_value": None}, delete_bucket={"return_value": None})

		self.bucket.on_trash()

		delete_bucket.assert_not_called()

	def test_revoking_leaves_the_bucket_alone(self):
		_, delete_key, delete_bucket = self.answers(
			key={"return_value": KEY},
			delete_key={"return_value": None},
			delete_bucket={"return_value": None},
		)

		self.bucket.revoke_credentials()

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

		credentials = self.bucket.rotate_credentials()

		self.assertEqual(credentials.access_key, KEY["accessKeyId"])
		self.assertEqual(order, ["create", "delete"])
		delete_key.assert_called_once_with(old["accessKeyId"])
		allow.assert_called_once_with(BUCKET_ID, KEY["accessKeyId"])

	def test_a_rotated_key_replaces_the_one_on_the_record(self):
		"""The record is the only place the new secret is kept."""
		self.answers(
			bucket={"return_value": {"id": BUCKET_ID}},
			key={"return_value": None},
			create_key={"return_value": KEY},
			allow_bucket_key={"return_value": {}},
			delete_key={"return_value": None},
		)

		credentials = self.bucket.rotate_credentials()

		self.assertEqual(self.bucket.access_key, credentials.access_key)
		self.assertEqual(self.bucket.secret_access_key, credentials.secret_access_key)
		self.saved.assert_called_once()

	def test_rotating_a_bucket_with_no_key_yet_just_issues_one(self):
		_, _, _, _, delete_key = self.answers(
			bucket={"return_value": {"id": BUCKET_ID}},
			key={"return_value": None},
			create_key={"return_value": KEY},
			allow_bucket_key={"return_value": {}},
			delete_key={"return_value": None},
		)

		self.assertEqual(self.bucket.rotate_credentials().access_key, KEY["accessKeyId"])
		delete_key.assert_not_called()

	def test_a_bucket_no_key_could_be_made_for_is_taken_back_out(self):
		"""Nobody was handed a way in, so leaving it would leave something unreachable."""
		_, _, _, create_key, delete_bucket = self.answers(
			create_bucket={"return_value": {"id": BUCKET_ID}},
			add_bucket_alias={"return_value": {}},
			bucket={"return_value": {"id": BUCKET_ID}},
			create_key={"side_effect": Error("CreateKey answered 500: nope")},
			delete_bucket={"return_value": None},
		)

		with self.assertRaises(Error):
			self.bucket.provision()

		create_key.assert_called_once()
		# Garage drops the name with the bucket, so nothing unnames it first.
		delete_bucket.assert_called_once_with(BUCKET_ID)

	def test_a_provisioned_bucket_keeps_its_key_on_the_record(self):
		_, _, _, _, _, delete_bucket = self.answers(
			create_bucket={"return_value": {"id": BUCKET_ID}},
			add_bucket_alias={"return_value": {}},
			bucket={"return_value": {"id": BUCKET_ID}},
			create_key={"return_value": KEY},
			allow_bucket_key={"return_value": {}},
			delete_bucket={"return_value": None},
		)

		self.bucket.provision()

		self.assertEqual(self.bucket.access_key, KEY["accessKeyId"])
		self.assertEqual(self.bucket.secret_access_key, KEY["secretAccessKey"])
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

		self.bucket.provision()

		allow.assert_called_once_with(BUCKET_ID, KEY["accessKeyId"])
		lookup.assert_not_called()

	def test_a_new_bucket_with_no_caps_asks_garage_for_no_quota(self):
		"""Every create would otherwise carry a pointless round trip."""
		quota = self.patch(Bucket, "apply_quota")
		self.bucket.flags.in_insert = True

		self.bucket.on_update()

		quota.assert_not_called()

	def test_a_new_bucket_with_a_cap_sends_it_once_the_bucket_exists(self):
		quota = self.patch(Bucket, "apply_quota")
		self.bucket.flags.in_insert = True
		self.bucket.max_size_gib = 2

		self.bucket.on_update()

		quota.assert_called_once()

	def test_every_bucket_operation_holds_that_bucket_name(self):
		"""Garage arbitrates the name, not the key behind it, so Cargo serialises the rest."""
		self.answers(
			bucket={"return_value": None},
			key={"return_value": None},
			delete_key={"return_value": None},
		)
		lock = self.patch(Bucket, "lock")

		self.bucket.on_trash()
		self.bucket.revoke_credentials()

		self.assertEqual(lock.call_count, 2)

	def test_a_failed_insert_undoes_exactly_what_provision_made(self):
		"""By id, not by name: the name could by then answer to someone else's bucket."""
		_, _, _, _, delete_bucket, delete_key = self.answers(
			create_bucket={"return_value": {"id": BUCKET_ID}},
			add_bucket_alias={"return_value": {}},
			create_key={"return_value": KEY},
			allow_bucket_key={"return_value": {}},
			delete_bucket={"return_value": None},
			delete_key={"return_value": None},
		)
		self.bucket.provision()

		self.bucket.discard_provisioned()

		delete_bucket.assert_called_once_with(BUCKET_ID)
		delete_key.assert_called_once_with(KEY["accessKeyId"])

	def test_nothing_is_undone_for_a_document_that_provisioned_nothing(self):
		"""A name Garage refused never provisioned here, and its bucket belongs to the winner."""
		delete_bucket, delete_key = self.answers(
			delete_bucket={"return_value": None}, delete_key={"return_value": None}
		)

		self.bucket.discard_provisioned()

		delete_bucket.assert_not_called()
		delete_key.assert_not_called()

	def test_an_undo_that_fails_is_logged_not_raised(self):
		"""Raising here would replace whatever refused the insert with a Garage error."""
		self.bucket.flags.provisioned = frappe._dict(bucket_id=BUCKET_ID, access_key=KEY["accessKeyId"])
		self.answers(delete_bucket={"side_effect": Error("DeleteBucket answered 500: nope")})
		log_error = self.patch(frappe, "log_error")

		self.bucket.discard_provisioned()

		log_error.assert_called_once()
