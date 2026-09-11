# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

import base64
import hashlib
import hmac
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import frappe
from frappe.integrations.doctype.webhook.webhook import (
	WEBHOOK_SECRET_HEADER,
	get_webhook_data,
	get_webhook_headers,
)
from frappe.tests import IntegrationTestCase
from frappe.utils.password import (
	delete_all_passwords_for,
	remove_encrypted_password,
	set_encrypted_password,
)

from cargo.client_models import GATEWAY, STORAGE
from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
	CLUSTER_SECRETS,
	WEBHOOK_ENDPOINT,
	ObjectStorageCluster,
	configure_storage_cluster_webhook,
)
from cargo.object_storage.garage.client import Client
from cargo.object_storage.garage.setup import Setup
from cargo.testing import use_test_settings

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class IntegrationTestObjectStorageCluster(IntegrationTestCase):
	"""Releasing machines: a failed run leaves them alone, so the operator does it by hand."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		self.cluster = frappe.get_doc({"doctype": "Object Storage Cluster"}).insert()
		# vm_id is unique across the table, so it cannot be a fixed string per role.
		self.vm_ids: dict[str, str] = {}
		self.gateway = self.add_machine(GATEWAY)
		self.storage = self.add_machine(STORAGE)

	def add_machine(self, role: str) -> str:
		vm_id = f"vm-{role}-{frappe.generate_hash(length=8)}"
		machine = frappe.get_doc(
			{
				"doctype": "Machine",
				"reference_doctype": self.cluster.doctype,
				"reference_name": self.cluster.name,
				"role": role,
				"disk_size_gb": 20,
				"vm_id": vm_id,
				"address": "10.0.0.1",
				"status": "Running",
			}
		).insert()
		self.vm_ids[machine.name] = vm_id
		self.cluster.append("machines", {"machine": machine.name, "role": role})
		self.cluster.save()

		return machine.name

	def _nothing_joined(self):
		"""Garage was never installed here, so nothing answers its address."""
		return patch.object(Setup, "healthy_nodes", return_value=set())

	def machine_names(self) -> set[str]:
		return {row.machine for row in frappe.get_doc("Object Storage Cluster", self.cluster.name).machines}

	def test_releasing_terminates_only_the_named_machines(self):
		with patch("cargo.atlas_client.AtlasClient") as atlas, self._nothing_joined():
			self.cluster.release_machines([self.storage])

		atlas.from_settings.return_value.terminate_vm.assert_called_once_with(self.vm_ids[self.storage])
		self.assertEqual(self.machine_names(), {self.gateway})
		self.assertEqual(frappe.db.get_value("Machine", self.storage, "status"), "Terminated")

	def test_the_machine_row_outlives_the_release(self):
		with patch("cargo.atlas_client.AtlasClient"), self._nothing_joined():
			self.cluster.release_machines([self.storage])

		self.assertTrue(frappe.db.exists("Machine", self.storage))

	def test_a_machine_of_another_cluster_is_refused(self):
		with patch("cargo.atlas_client.AtlasClient") as atlas:
			with self.assertRaises(frappe.ValidationError):
				self.cluster.release_machines([self.storage, "not-ours"])

		atlas.from_settings.assert_not_called()
		self.assertEqual(self.machine_names(), {self.gateway, self.storage})

	def test_releasing_nothing_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.cluster.release_machines([])

	def test_a_cluster_being_set_up_cannot_be_released(self):
		self.cluster.db_set("status", "Setting Up")
		self.cluster.reload()

		with self.assertRaises(frappe.ValidationError):
			self.cluster.release_machines([self.storage])

		self.assertEqual(self.machine_names(), {self.gateway, self.storage})


class IntegrationTestClusterCredentials(IntegrationTestCase):
	"""Cargo mints its cluster's secrets itself, on the way in, and asks nobody for them."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		self.cluster = frappe.get_doc({"doctype": "Object Storage Cluster"}).insert()

	def secrets(self) -> dict[str, str]:
		return {name: self.cluster.get_password(name) for name in CLUSTER_SECRETS}

	def test_a_cluster_is_born_with_its_secrets(self):
		minted = self.secrets()

		self.assertEqual(len(set(minted.values())), len(CLUSTER_SECRETS))
		self.assertTrue(all(minted.values()))

	def test_saving_again_keeps_the_first_set(self):
		"""Every node of a cluster boots with the same secrets; re-minting would split it."""
		first = self.secrets()
		self.cluster.save()

		self.assertEqual(first, self.secrets())

	def test_a_node_cannot_be_installed_before_the_secrets_exist(self):
		delete_all_passwords_for("Object Storage Cluster", self.cluster.name)
		self.cluster.reload()

		with self.assertRaises(frappe.ValidationError) as raised:
			self.cluster.garage_setup.secrets

		self.assertIn("rpc_secret", str(raised.exception))

	def test_a_system_manager_can_see_the_admin_token(self):
		self.assertEqual(self.cluster.reveal_admin_token(), self.secrets()["admin_token"])

	def test_nobody_else_can_see_the_admin_token(self):
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")

		with self.assertRaises(frappe.PermissionError):
			self.cluster.reveal_admin_token()


class IntegrationTestLiveClusterRelease(IntegrationTestCase):
	"""A cluster that has served holds data, so releasing a node can cost a copy."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		self.cluster = frappe.get_doc(
			{
				"doctype": "Object Storage Cluster",
				"replication_factor": 2,
				"rpc_secret": "rpc",
				"admin_token": "admin",
				"metrics_token": "metrics",
			}
		).insert()
		self.vm_ids: dict[str, str] = {}
		self.gateway = self.add_machine(GATEWAY)
		self.storage = [self.add_machine(STORAGE) for _ in range(3)]
		self.cluster.db_set({"status": "Active", "activated_on": frappe.utils.now_datetime()})
		self.cluster.reload()

	add_machine = IntegrationTestObjectStorageCluster.add_machine

	@contextmanager
	def _joined(self, names: list[str]):
		with patch.object(Setup, "healthy_nodes", return_value=set(names)):
			yield

	def test_the_gateway_cannot_be_released(self):
		with self._joined(self.storage), self.assertRaises(frappe.ValidationError):
			self.cluster.release_machines([self.gateway])

	def test_a_serving_node_cannot_be_released(self):
		"""Releasing aids setup. A node that joined is working, not broken."""
		with self._joined(self.storage), self.assertRaises(frappe.ValidationError):
			self.cluster.release_machines([self.storage[0]])

		self.assertEqual(frappe.db.get_value("Machine", self.storage[0], "status"), "Running")

	def test_one_serving_node_refuses_the_whole_request(self):
		with self._joined(self.storage[:1]), self.assertRaises(frappe.ValidationError):
			self.cluster.release_machines([self.storage[0], self.storage[1]])

		self.assertEqual(frappe.db.get_value("Machine", self.storage[1], "status"), "Running")

	def test_a_node_that_never_joined_can_be_released(self):
		with (
			self._joined(self.storage[:2]),
			patch("cargo.atlas_client.AtlasClient"),
		):
			self.cluster.release_machines([self.storage[2]])

		self.assertEqual(frappe.db.get_value("Machine", self.storage[2], "status"), "Terminated")

	def test_a_silent_garage_refuses_the_whole_thing(self):
		"""healthy_nodes() returns an empty set on Error, which must not read as
		"nothing is serving" on a cluster that has served."""
		with self._joined([]), self.assertRaises(frappe.ValidationError):
			self.cluster.release_machines([self.storage[0]])

	def test_a_cluster_that_never_served_may_release_a_broken_node(self):
		"""The setup case: Garage is unreachable because the run failed, and the machines
		it left behind are the whole reason this exists."""
		self.cluster.db_set("activated_on", None)
		self.cluster.reload()

		with self._joined([]), patch("cargo.atlas_client.AtlasClient"):
			self.cluster.release_machines([self.storage[0]])

		self.assertEqual(frappe.db.get_value("Machine", self.storage[0], "status"), "Terminated")


class IntegrationTestClusterPeers(IntegrationTestCase):
	"""Recording peers: one read of Garage, and only the machines that joined are written to."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		self.cluster = frappe.get_doc(
			{
				"doctype": "Object Storage Cluster",
				"rpc_secret": "rpc",
				"admin_token": "admin",
				"metrics_token": "metrics",
			}
		).insert()
		self.vm_ids: dict[str, str] = {}
		self.gateway = self.add_machine(GATEWAY)
		self.storage = self.add_machine(STORAGE)

	add_machine = IntegrationTestObjectStorageCluster.add_machine

	@contextmanager
	def _garage(self, joined: list[str]):
		"""Garage answering that `joined` are up, each tagged with its machine name."""
		nodes = {
			"nodes": [
				{"id": f"node{index}", "addr": "10.0.0.1:3901", "isUp": True, "role": {"tags": [name]}}
				for index, name in enumerate(joined)
			]
		}
		with (
			patch.object(Client, "status", return_value=nodes) as status,
			patch.object(Client, "layout", return_value={"stagedRoleChanges": []}) as layout,
			patch.object(Setup, "record_peers") as record,
		):
			yield SimpleNamespace(status=status, layout=layout), record

	def test_peers_are_read_once_and_written_to_joined_machines(self):
		with self._garage([self.gateway, self.storage]) as (admin, record):
			self.cluster.record_cluster_peers()

		self.assertEqual(admin.status.call_count, 1)
		self.assertEqual(admin.layout.call_count, 1)
		self.assertEqual(
			{call.args[0]["name"] for call in record.call_args_list}, {self.gateway, self.storage}
		)

	def test_a_machine_that_never_joined_is_not_written_to(self):
		with self._garage([self.gateway]) as (_, record):
			self.cluster.record_cluster_peers()

		self.assertEqual({call.args[0]["name"] for call in record.call_args_list}, {self.gateway})

	def test_nothing_is_written_when_garage_reports_no_peers(self):
		with self._garage([]) as (_, record):
			self.cluster.record_cluster_peers()

		record.assert_not_called()


class IntegrationTestClusterWebhook(IntegrationTestCase):
	"""A cluster reports its own status: inserting one points a webhook at Central."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		self.settings = frappe.get_doc("Cargo Settings")
		# Set per test: a test that takes it away must not leave the next one without one.
		self.set_secret("central-knows-this")

	def set_secret(self, secret: str) -> None:
		"""Written straight to the password store: this site's Cargo Settings is half filled
		in, so saving the whole thing would ask for every other field."""
		set_encrypted_password("Cargo Settings", "Cargo Settings", secret, "central_webhook_secret")
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")

	def webhook_of(self, cluster: ObjectStorageCluster):
		return frappe.get_doc("Webhook", f"object_storage_cluster-{cluster.name}")

	def insert_cluster(self) -> ObjectStorageCluster:
		return frappe.get_doc({"doctype": "Object Storage Cluster"}).insert()

	def test_a_new_cluster_gets_a_webhook_pointed_at_central(self):
		webhook = self.webhook_of(self.insert_cluster())

		self.assertEqual(webhook.webhook_doctype, "Object Storage Cluster")
		self.assertEqual(webhook.webhook_docevent, "on_update")
		self.assertEqual(webhook.request_method, "POST")
		self.assertTrue(webhook.request_url.endswith(WEBHOOK_ENDPOINT))
		self.assertTrue(webhook.request_url.startswith(self.settings.central_url))

	def test_only_a_settled_cluster_is_reported(self):
		webhook = self.webhook_of(self.insert_cluster())
		condition = webhook.condition

		self.assertTrue(frappe.safe_eval(condition, eval_locals={"doc": frappe._dict(status="Active")}))
		self.assertTrue(frappe.safe_eval(condition, eval_locals={"doc": frappe._dict(status="Failed")}))
		self.assertFalse(frappe.safe_eval(condition, eval_locals={"doc": frappe._dict(status="Setting Up")}))

	def test_the_report_names_the_region_and_the_cluster_state(self):
		cluster = self.insert_cluster()
		cluster.db_set("status", "Active")
		cluster.reload()

		report = get_webhook_data(cluster, self.webhook_of(cluster))

		self.assertEqual(report["region"], self.settings.region)
		self.assertEqual(report["region_id"], self.settings.region_id)
		self.assertEqual(report["service"], "storage")
		self.assertEqual(report["status"], "Active")

	def test_a_cargo_with_no_webhook_secret_makes_no_cluster(self):
		"""Nothing may post to Central unauthenticated, so the cluster does not get made."""
		remove_encrypted_password("Cargo Settings", "Cargo Settings", "central_webhook_secret")
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")

		with self.assertRaises(frappe.ValidationError):
			self.insert_cluster()

	def test_the_report_is_signed_with_the_secret_central_knows_this_cargo_by(self):
		"""Signed, not sent: the secret itself never leaves Cargo."""
		self.set_secret("shared-with-central")
		cluster = self.insert_cluster()
		webhook = self.webhook_of(cluster)

		self.assertTrue(webhook.enabled)
		self.assertTrue(webhook.enable_security)
		self.assertEqual(webhook.get_password("webhook_secret"), "shared-with-central")

		data = get_webhook_data(cluster, webhook)
		signature = base64.b64encode(
			hmac.new(b"shared-with-central", frappe.as_json(data).encode(), hashlib.sha256).digest()
		)

		# Frappe hands the signature back as bytes on some versions and str on others.
		header = get_webhook_headers(cluster, webhook)[WEBHOOK_SECRET_HEADER]

		self.assertEqual(frappe.as_unicode(header), signature.decode())

	def test_reconfiguring_rotates_the_secret_and_keeps_one_webhook(self):
		cluster = self.insert_cluster()
		self.set_secret("rotated")
		configure_storage_cluster_webhook(cluster)

		self.assertEqual(frappe.db.count("Webhook", {"name": f"object_storage_cluster-{cluster.name}"}), 1)
		self.assertEqual(self.webhook_of(cluster).get_password("webhook_secret"), "rotated")
