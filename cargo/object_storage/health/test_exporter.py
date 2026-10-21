# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.client_models import GATEWAY, STORAGE
from cargo.object_storage.doctype.object_storage_cluster import object_storage_cluster
from cargo.object_storage.doctype.object_storage_cluster.setup import Setup
from cargo.object_storage.health import install_cluster_metrics_exporters, install_garage_metrics_exporters
from cargo.telemetry import vector as vector_module
from cargo.testing import use_test_settings

INFO = {"endpoint": "https://datum.test", "token": "datum-jwt"}


class IntegrationTestMetricsExporter(IntegrationTestCase):
	"""Vector on every node: host metrics and Garage's own, pushed to datum with the region's token."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		self.cluster = frappe.get_doc({"doctype": "Object Storage Cluster"}).insert()
		# The cluster refuses to save until it has been minted its credentials.
		for field, value in (
			("rpc_secret", "rpc"),
			("admin_token", "admin"),
			("metrics_token", "cluster-metrics-token"),
		):
			self.cluster.db_set(field, value)
		self.cluster.reload()
		self.gateway = self.add_machine(GATEWAY, "10.0.0.1")
		self.storage = self.add_machine(STORAGE, "10.0.0.2")

	def add_machine(self, role: str, ipv4: str) -> str:
		machine = frappe.get_doc(
			{
				"doctype": "Machine",
				"reference_doctype": self.cluster.doctype,
				"reference_name": self.cluster.name,
				"role": role,
				"disk_size_gb": 20,
				"vm_id": f"vm-{frappe.generate_hash(length=8)}",
				"address": ipv4,
				"status": "Running",
			}
		).insert()
		self.cluster.append("machines", {"machine": machine.name, "role": role})
		self.cluster.save()

		return machine.name

	def install(self, run=None) -> MagicMock:
		run = run or MagicMock()
		with (
			patch.object(vector_module, "get_metrics_info", return_value=INFO),
			patch.object(Setup, "run", run),
		):
			install_cluster_metrics_exporters(self.cluster.name)

		return run

	def sent_to(self, machine: str) -> str:
		return next(call.args[1] for call in self.install().call_args_list if call.args[0]["name"] == machine)

	def test_every_node_gets_the_exporter(self):
		run = self.install()

		self.assertEqual({call.args[0]["name"] for call in run.call_args_list}, {self.gateway, self.storage})

	def test_the_node_is_handed_datums_endpoint_and_token(self):
		sent = self.install().call_args_list[0].args[1]

		self.assertIn("export DATUM_ENDPOINT=https://datum.test", sent)
		self.assertIn("export DATUM_TOKEN=datum-jwt", sent)

	def test_the_node_labels_its_metrics_with_where_it_runs(self):
		sent = self.sent_to(self.storage)

		self.assertIn(f"export CLUSTER={self.cluster.name}", sent)
		self.assertIn(f"export MACHINE={self.storage}", sent)
		self.assertIn(f"export ROLE={STORAGE}", sent)

	def test_the_node_scrapes_garage_itself_with_the_clusters_settings(self):
		self.cluster.db_set("metrics_scrape_seconds", 120)
		sent = self.sent_to(self.gateway)

		self.assertIn(f"export GARAGE_ADMIN_PORT={self.cluster.admin_port}", sent)
		self.assertIn("export GARAGE_METRICS_TOKEN=cluster-metrics-token", sent)
		self.assertIn("export GARAGE_SCRAPE_SECONDS=120", sent)

	def test_histograms_reach_datum_as_sum_and_count_only(self):
		sent = self.sent_to(self.storage)

		self.assertIn('type = "prometheus_scrape"', sent)
		self.assertIn('sum.name = name + "_sum"', sent)
		self.assertIn('count.name = name + "_count"', sent)
		self.assertIn("del(.aggregated_histogram)", sent)

	def test_one_unreachable_node_does_not_keep_the_token_from_the_rest(self):
		run = self.install(run=MagicMock(side_effect=[RuntimeError("ssh refused"), ""]))

		self.assertEqual(run.call_count, 2)

	def test_a_new_token_reaches_every_live_cluster_once(self):
		self.cluster.db_set("activated_on", frappe.utils.now_datetime())
		with patch.object(frappe, "enqueue") as enqueue:
			install_garage_metrics_exporters()

		enqueued = [call for call in enqueue.call_args_list if call.kwargs["cluster"] == self.cluster.name]
		self.assertEqual(len(enqueued), 1)
		self.assertTrue(enqueued[0].kwargs["deduplicate"])


class IntegrationTestScrapeInterval(IntegrationTestCase):
	"""The interval lives on the cluster, and a change reaches a live cluster's nodes."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		self.cluster = frappe.get_doc({"doctype": "Object Storage Cluster"}).insert()

	def test_defaults_to_one_minute(self):
		self.assertEqual(self.cluster.metrics_scrape_seconds, 60)

	def test_refuses_an_interval_below_one_minute(self):
		self.cluster.metrics_scrape_seconds = 59

		self.assertRaises(frappe.ValidationError, self.cluster.save)

	def test_a_new_interval_reinstalls_vector_on_a_live_cluster(self):
		self.cluster.db_set("activated_on", frappe.utils.now_datetime())
		self.cluster.metrics_scrape_seconds = 120
		with patch.object(object_storage_cluster, "queue_cluster_metrics_exporters") as queue:
			self.cluster.save()

		queue.assert_called_once_with(self.cluster.name)

	def test_a_cluster_that_has_not_served_waits_for_setup(self):
		self.cluster.metrics_scrape_seconds = 120
		with patch.object(object_storage_cluster, "queue_cluster_metrics_exporters") as queue:
			self.cluster.save()

		queue.assert_not_called()
