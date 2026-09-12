# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.password import remove_encrypted_password

from cargo.client_models import GATEWAY, STORAGE
from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
	ObjectStorageCluster,
)
from cargo.object_storage.spawn import (
	CONFIG_KEY,
	LOCK_NAME,
	MAX_SETUP_ATTEMPTS,
	ensure_cluster,
)
from cargo.testing import use_test_settings

CONFIG = {
	"storage_node_count": 3,
	"replication_factor": 3,
	GATEWAY: {"cpu": 2, "ram_gb": 4, "disk_gb": 20},
	STORAGE: {"cpu": 2, "ram_gb": 4, "disk_gb": 100},
}


class SpawnTestCase(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		# The transaction is rolled back once per class, so a test sees what the one before it
		# left. Everything here turns on how many clusters the region has, so it starts at none.
		for doctype in ("Object Storage Node", "Machine", "Object Storage Cluster"):
			frappe.db.delete(doctype)

	def configured(self, config: dict | None = CONFIG):
		"""Site config for one test. `frappe.conf` is a dict, so it is patched in place."""
		return patch.dict(frappe.local.conf, {CONFIG_KEY: config})

	def atlas(self, image_id: str = "img-9"):
		"""Atlas hands out one system image and a machine per call."""
		client = patch("cargo.atlas_client.AtlasClient.from_settings").start()
		self.addCleanup(patch.stopall)
		client.return_value.find_system_image.return_value = image_id
		client.return_value.create_vm.side_effect = lambda **kwargs: {
			"id": f"vm-{frappe.generate_hash(length=8)}"
		}

		return client.return_value

	def clusters(self) -> list[str]:
		return frappe.get_all("Object Storage Cluster", pluck="name")

	def auto_cluster(self) -> ObjectStorageCluster:
		return frappe.get_doc("Object Storage Cluster", {"auto_spawn": 1})

	def add_machine(self, cluster: ObjectStorageCluster, role: str, status: str = "Running") -> str:
		machine = frappe.get_doc(
			{
				"doctype": "Machine",
				"reference_doctype": cluster.doctype,
				"reference_name": cluster.name,
				"role": role,
				"disk_size_gb": 20,
				"vm_id": f"vm-{frappe.generate_hash(length=8)}",
				"address": "fd00::1",
				"status": status,
			}
		).insert()
		cluster.append("machines", {"machine": machine.name, "role": role})
		cluster.save()

		return machine.name

	def fill(self, cluster: ObjectStorageCluster, status: str = "Running") -> None:
		"""The cluster the config asks for, already booted."""
		self.add_machine(cluster, GATEWAY, status)
		for _ in range(CONFIG["storage_node_count"]):
			self.add_machine(cluster, STORAGE, status)


class IntegrationTestSpawnCreation(SpawnTestCase):
	"""Which hosts build a cluster, and how many they build."""

	def test_a_host_with_no_config_builds_nothing(self):
		with self.configured(None):
			ensure_cluster()

		self.assertEqual(self.clusters(), [])

	def test_an_unusable_config_builds_nothing(self):
		with self.configured({"storage_node_count": 0}):
			ensure_cluster()

		self.assertEqual(self.clusters(), [])

	def test_too_few_storage_nodes_for_a_full_copy_is_refused(self):
		"""Rented machines that every setup run would then refuse. Nothing is built."""
		with self.configured(CONFIG | {"storage_node_count": 2}):
			ensure_cluster()

		self.assertEqual(self.clusters(), [])

	def test_the_cluster_needs_as_many_copies_as_the_config_asks_for(self):
		with self.configured(CONFIG | {"storage_node_count": 2, "replication_factor": 2}):
			ensure_cluster()

		self.assertEqual(self.auto_cluster().replication_factor, 2)

	def test_a_run_already_under_way_is_not_joined_by_another(self):
		from frappe.utils.synchronization import filelock

		with self.configured(), filelock(LOCK_NAME, timeout=0):
			ensure_cluster()

		self.assertEqual(self.clusters(), [])

	def test_a_fresh_host_builds_one_cluster(self):
		with self.configured():
			ensure_cluster()

		cluster = self.auto_cluster()
		self.assertEqual(cluster.status, "Draft")
		self.assertEqual(cluster.machines, [])

	def test_a_host_missing_its_settings_builds_nothing(self):
		frappe.db.set_single_value("Cargo Settings", "central_url", "")
		remove_encrypted_password("Cargo Settings", "Cargo Settings", "central_webhook_secret")
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")

		with self.configured():
			ensure_cluster()

		self.assertEqual(self.clusters(), [])

	def test_a_cluster_made_by_hand_is_never_joined_by_a_second(self):
		frappe.get_doc({"doctype": "Object Storage Cluster"}).insert()

		with self.configured():
			ensure_cluster()

		self.assertEqual(len(self.clusters()), 1)
		self.assertFalse(frappe.db.exists("Object Storage Cluster", {"auto_spawn": 1}))

	def test_the_cluster_is_built_once_and_not_again(self):
		self.atlas()
		with self.configured():
			ensure_cluster()
			ensure_cluster()

		self.assertEqual(len(self.clusters()), 1)


class IntegrationTestSpawnMachines(SpawnTestCase):
	"""Asking Atlas for the machines the config describes."""

	def setUp(self):
		super().setUp()
		self.cluster = frappe.get_doc({"doctype": "Object Storage Cluster", "auto_spawn": 1}).insert()

	def roles(self) -> list[str]:
		return [row.role for row in self.auto_cluster().machines]

	def test_the_gateway_is_asked_for_first(self):
		self.atlas()
		with self.configured():
			ensure_cluster()

		self.assertEqual(self.roles(), [GATEWAY] + [STORAGE] * CONFIG["storage_node_count"])

	def test_each_machine_is_the_size_the_config_asks_for(self):
		client = self.atlas()
		with self.configured():
			ensure_cluster()

		sizes = {
			(call.kwargs["vcpus"], call.kwargs["memory_mib"], call.kwargs["disk_mib"])
			for call in client.create_vm.call_args_list
		}
		self.assertEqual(sizes, {(2, 4 * 1024, 20 * 1024), (2, 4 * 1024, 100 * 1024)})

	def test_the_base_image_comes_from_atlas_and_not_a_name(self):
		client = self.atlas(image_id="img-42")
		with self.configured():
			ensure_cluster()

		for machine_call in client.create_vm.call_args_list:
			self.assertEqual(machine_call.kwargs["image_id"], "img-42")

	def test_a_region_with_no_system_image_rents_nothing(self):
		client = self.atlas(image_id=None)
		with self.configured():
			ensure_cluster()

		client.create_vm.assert_not_called()
		self.assertEqual(self.roles(), [])

	def test_a_machine_atlas_refuses_keeps_the_ones_it_built(self):
		client = self.atlas()
		client.create_vm.side_effect = [{"id": "vm-1"}, {"id": "vm-2"}, RuntimeError("no capacity")]
		with self.configured():
			ensure_cluster()

		self.assertEqual(self.roles(), [GATEWAY, STORAGE])
		self.assertEqual(self.auto_cluster().status, "Draft")

	def test_the_next_run_asks_only_for_what_is_missing(self):
		self.add_machine(self.cluster, GATEWAY)
		self.add_machine(self.cluster, STORAGE)

		client = self.atlas()
		with self.configured():
			ensure_cluster()

		self.assertEqual(client.create_vm.call_count, CONFIG["storage_node_count"] - 1)

	def test_a_full_cluster_is_asked_for_nothing_more(self):
		self.fill(self.cluster)

		client = self.atlas()
		with self.configured(), patch.object(ObjectStorageCluster, "setup"):
			ensure_cluster()

		client.create_vm.assert_not_called()

	def test_a_machine_that_would_not_boot_stops_the_run(self):
		self.add_machine(self.cluster, GATEWAY, status="Broken")

		client = self.atlas()
		with self.configured():
			ensure_cluster()

		client.create_vm.assert_not_called()
		client.terminate_vm.assert_not_called()
		self.assertIn("did not come up", self.auto_cluster().error)


class IntegrationTestSpawnSetup(SpawnTestCase):
	"""When the cluster is handed to the setup flow, and when it is not."""

	def setUp(self):
		super().setUp()
		self.cluster = frappe.get_doc({"doctype": "Object Storage Cluster", "auto_spawn": 1}).insert()

	def running_setup(self):
		return patch.object(ObjectStorageCluster, "setup")

	def test_a_booted_cluster_is_set_up(self):
		self.fill(self.cluster)

		self.atlas()
		with self.configured(), self.running_setup() as setup:
			ensure_cluster()

		setup.assert_called_once()

	def test_a_cluster_still_booting_waits(self):
		self.fill(self.cluster, status="Pending")

		self.atlas()
		with self.configured(), self.running_setup() as setup:
			ensure_cluster()

		setup.assert_not_called()

	def test_a_cluster_added_by_hand_is_never_set_up(self):
		manual = frappe.get_doc({"doctype": "Object Storage Cluster"}).insert()
		self.fill(manual)
		self.fill(self.cluster)
		self.cluster.db_set("status", "Active")

		self.atlas()
		with self.configured(), self.running_setup() as setup:
			ensure_cluster()

		setup.assert_not_called()

	def test_a_run_under_way_is_left_alone(self):
		self.fill(self.cluster)
		self.cluster.db_set("status", "Setting Up")

		self.atlas()
		with self.configured(), self.running_setup() as setup:
			ensure_cluster()

		setup.assert_not_called()


class IntegrationTestSpawnRetry(SpawnTestCase):
	"""A failed run rents nothing, so it is worth trying again — up to a point."""

	def setUp(self):
		super().setUp()
		self.cluster = frappe.get_doc({"doctype": "Object Storage Cluster", "auto_spawn": 1}).insert()
		self.fill(self.cluster)
		self.cluster.db_set("status", "Failed")

	def run_once(self):
		self.atlas()
		with self.configured(), patch.object(ObjectStorageCluster, "setup") as setup:
			ensure_cluster()

		return setup

	def test_a_failed_run_is_tried_again(self):
		self.run_once().assert_called_once()
		self.assertEqual(self.auto_cluster().auto_setup_attempts, 1)

	def test_trying_again_stops_once_the_budget_is_spent(self):
		for _ in range(MAX_SETUP_ATTEMPTS):
			self.run_once()

		self.run_once().assert_not_called()
		self.assertEqual(self.auto_cluster().auto_setup_attempts, MAX_SETUP_ATTEMPTS)

	def test_a_cluster_that_served_is_left_to_its_operator(self):
		self.cluster.db_set("activated_on", frappe.utils.now_datetime())

		self.run_once().assert_not_called()

	def test_serving_arms_the_budget_again(self):
		self.cluster.db_set("auto_setup_attempts", MAX_SETUP_ATTEMPTS)
		self.cluster.reload()
		self.cluster.mark_cluster_status("Active")

		self.assertEqual(self.auto_cluster().auto_setup_attempts, 0)
