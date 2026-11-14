# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.client_models import GATEWAY, STORAGE
from cargo.object_storage.doctype.object_storage_cluster import object_storage_cluster
from cargo.object_storage.doctype.object_storage_cluster.setup import Setup
from cargo.object_storage.health import install_cluster_metrics_exporters
from cargo.testing import use_test_settings

SETUP_MODULE = "cargo.object_storage.doctype.object_storage_cluster.setup"


class IntegrationTestMetricsExporter(IntegrationTestCase):
	"""The Datum agent on every node: machine metrics and Garage's own, sent with the node's own token."""

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
			patch(f"{SETUP_MODULE}.AtlasClient.from_settings"),
			patch(f"{SETUP_MODULE}.generate_machine_token", return_value="machine-token"),
			patch.object(Setup, "run", run),
		):
			install_cluster_metrics_exporters(self.cluster.name)

		return run

	def sent_to(self, machine: str) -> list[str]:
		return [call.args[1] for call in self.install().call_args_list if call.args[0]["name"] == machine]

	def test_every_node_gets_the_exporter(self):
		run = self.install()

		self.assertEqual({call.args[0]["name"] for call in run.call_args_list}, {self.gateway, self.storage})

	def test_the_node_fetches_its_own_token_before_the_agent_installs(self):
		refresh, agent = self.sent_to(self.storage)

		self.assertIn("datum-refresh.timer", refresh)
		self.assertIn("Installs the Datum agent", agent)
		self.assertNotIn("export DATUM_TOKEN=", agent)

	def test_only_the_gateway_sends_the_nginx_error_log(self):
		self.assertIn("logs_nginx", self.sent_to(self.gateway)[1])
		self.assertNotIn("logs_nginx", self.sent_to(self.storage)[1])

	def test_the_node_scrapes_garage_itself_with_the_clusters_settings(self):
		self.cluster.db_set("metrics_scrape_seconds", 120)
		sent = self.sent_to(self.gateway)[1]

		self.assertIn(f"GARAGE_ADMIN_ADDRESS=10.0.0.1:{self.cluster.admin_port}", sent)
		self.assertIn("GARAGE_METRICS_TOKEN=cluster-metrics-token", sent)
		self.assertIn("GARAGE_SCRAPE_SECONDS=120", sent)

	def test_histograms_reach_datum_as_sum_and_count_only(self):
		sent = self.sent_to(self.storage)[1]

		self.assertIn("type: prometheus_scrape", sent)
		self.assertIn('sum.name = name + "_sum"', sent)
		self.assertIn('count.name = name + "_count"', sent)
		self.assertIn("del(.aggregated_histogram)", sent)

	def test_one_unreachable_node_does_not_keep_the_exporter_from_the_rest(self):
		run = self.install(run=MagicMock(side_effect=[RuntimeError("ssh refused"), "", ""]))

		self.assertEqual(run.call_count, 3)


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
