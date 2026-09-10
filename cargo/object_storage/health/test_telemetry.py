# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

from cargo.client_models import GATEWAY, STORAGE
from cargo.object_storage.health import telemetry as telemetry_module
from cargo.object_storage.health.telemetry import Telemetry, parse_metrics

METRICS = """\
# HELP cluster_healthy Whether all storage nodes are connected
# TYPE cluster_healthy gauge
cluster_healthy 1
garage_local_disk_avail{volume="data"} 58879201280
api_s3_request_counter{api_endpoint="PutObject"} 12
api_s3_request_duration_bucket{api_endpoint="PutObject",le="0.005"} 3
api_s3_request_duration_bucket{api_endpoint="PutObject",le="+Inf"} 12
api_s3_request_duration_sum{api_endpoint="PutObject"} 0.42
api_s3_request_duration_count{api_endpoint="PutObject"} 12
"""

INFO = {"endpoint": "https://datum.test", "token": "datum-jwt"}


class IntegrationTestMetricsParser(IntegrationTestCase):
	"""Prometheus text into datum samples."""

	def samples(self, text: str = METRICS) -> list[dict]:
		return parse_metrics(text, {"machine": "node-1"}, "2026-09-09T10:00:00")

	def test_histogram_buckets_are_dropped(self):
		"""They are the bulk of the payload and datum.samples has no TTL."""
		self.assertFalse([s for s in self.samples() if s["metric"].endswith("_bucket")])

	def test_the_sum_and_count_survive_so_averages_still_work(self):
		names = {sample["metric"] for sample in self.samples()}

		self.assertIn("garage_api_s3_request_duration_sum", names)
		self.assertIn("garage_api_s3_request_duration_count", names)

	def test_every_metric_is_prefixed(self):
		"""datum.samples is shared, where a bare `cluster_healthy` would collide."""
		self.assertTrue(all(sample["metric"].startswith("garage_") for sample in self.samples()))

	def test_an_already_prefixed_metric_is_not_prefixed_twice(self):
		names = {sample["metric"] for sample in self.samples()}

		self.assertIn("garage_local_disk_avail", names)
		self.assertNotIn("garage_garage_local_disk_avail", names)

	def test_garage_labels_survive_alongside_ours(self):
		disk = next(s for s in self.samples() if s["metric"] == "garage_local_disk_avail")

		self.assertEqual(disk["labels"], {"volume": "data", "machine": "node-1"})

	def test_our_labels_win_over_a_clashing_one(self):
		"""A node cannot claim to be another machine."""
		samples = parse_metrics('x_total{machine="liar"} 1', {"machine": "node-1"}, "T")

		self.assertEqual(samples[0]["labels"]["machine"], "node-1")

	def test_a_metric_with_no_labels_is_read(self):
		healthy = next(s for s in self.samples() if s["metric"] == "garage_cluster_healthy")

		self.assertEqual(healthy["value"], 1.0)

	def test_comments_and_junk_are_skipped(self):
		"""stderr is merged into the stream, so it cannot all be trusted."""
		self.assertEqual(self.samples("# HELP x\nbroken NaNsense\n\n"), [])

	def test_a_comma_inside_a_label_value_does_not_split_it(self):
		samples = parse_metrics('x_total{path="/a,b",id="7"} 3', {}, "T")

		self.assertEqual(samples[0]["labels"], {"path": "/a,b", "id": "7"})

	def test_a_trailing_timestamp_is_ignored(self):
		samples = parse_metrics("y_total 12 1757351400000", {}, "T")

		self.assertEqual(samples[0]["value"], 12.0)

	def test_every_sample_carries_the_scrape_timestamp(self):
		self.assertTrue(all(sample["ts"] == "2026-09-09T10:00:00" for sample in self.samples()))


class IntegrationTestTelemetry(IntegrationTestCase):
	"""Scraping each node over HTTP and relaying it to datum."""

	def setUp(self):
		frappe.set_user("Administrator")
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

	def response(self, text: str = METRICS, status: int = 200) -> MagicMock:
		reply = MagicMock()
		reply.text, reply.ok, reply.status_code = text, status < 400, status
		if status >= 400:
			reply.raise_for_status.side_effect = requests.HTTPError(f"{status}")
		return reply

	def ship(self, get=None, post=None):
		"""Ship with the network stubbed, returning the two call recorders."""
		get = get or MagicMock(return_value=self.response())
		post = post or MagicMock(return_value=self.response(text="", status=200))
		with (
			patch.object(telemetry_module, "get_metrics_info", return_value=INFO),
			patch.object(telemetry_module.requests, "get", get),
			patch.object(telemetry_module.requests, "post", post),
		):
			Telemetry(self.cluster).ship()

		return get, post

	def test_every_node_is_scraped_on_its_own_address(self):
		get, _ = self.ship()
		urls = {call.args[0] for call in get.call_args_list}

		self.assertEqual(
			urls,
			{
				f"http://10.0.0.1:{self.cluster.admin_port}/metrics",
				f"http://10.0.0.2:{self.cluster.admin_port}/metrics",
			},
		)

	def test_the_scrape_carries_the_cluster_metrics_token(self):
		get, _ = self.ship()

		self.assertEqual(
			get.call_args_list[0].kwargs["headers"],
			{"Authorization": "Bearer cluster-metrics-token"},
		)

	def test_the_scrape_is_bounded(self):
		"""A wedged node must not hold the cron open."""
		get, _ = self.ship()

		self.assertEqual(get.call_args_list[0].kwargs["timeout"], telemetry_module.SCRAPE_TIMEOUT)

	def test_samples_are_posted_to_datum_with_its_own_token(self):
		_, post = self.ship()

		self.assertEqual(post.call_args.args[0], "https://datum.test/v1/ingest")
		self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer datum-jwt"})

	def test_each_node_ships_separately_and_is_labelled(self):
		_, post = self.ship()
		machines = {
			sample["labels"]["machine"]
			for call in post.call_args_list
			for sample in call.kwargs["json"]["samples"]
		}

		self.assertEqual(post.call_count, 2)
		self.assertEqual(machines, {self.gateway, self.storage})

	def test_one_unreachable_node_does_not_stop_the_others(self):
		get = MagicMock(side_effect=[requests.ConnectionError("refused"), self.response()])
		_, post = self.ship(get=get)

		self.assertEqual(post.call_count, 1)

	def test_a_node_answering_401_is_skipped(self):
		get = MagicMock(return_value=self.response(status=401))
		_, post = self.ship(get=get)

		post.assert_not_called()

	def test_a_node_with_nothing_to_report_is_not_posted(self):
		get = MagicMock(return_value=self.response(text="# HELP only a comment\n"))
		_, post = self.ship(get=get)

		post.assert_not_called()

	def test_datum_refusing_the_write_is_not_retried(self):
		"""Datum drops writes by design: a gap in a chart beats a stalled cron."""
		post = MagicMock(return_value=self.response(text="rate limited", status=429))
		_, post = self.ship(post=post)

		self.assertEqual(post.call_count, 2)

	def test_shipping_without_datum_configured_scrapes_nothing(self):
		get = MagicMock()
		with (
			patch.object(telemetry_module, "get_metrics_info", side_effect=RuntimeError("no datum")),
			patch.object(telemetry_module.requests, "get", get),
			self.assertRaises(RuntimeError),
		):
			Telemetry(self.cluster).ship()

		get.assert_not_called()
