# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, call, patch
from urllib.error import URLError

from frappe.tests import UnitTestCase

from cargo.image_builder.conf import build_agent
from cargo.image_builder.conf.build_agent.models import BuildUnit, Release, ReleaseUnit

AGENT = "cargo.image_builder.conf.build_agent"


class UnitTestBuildAgentLogs(UnitTestCase):
	"""The agent reports only the journal lines Central does not have yet."""

	def setUp(self):
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		patch(f"{AGENT}.models.SECRETS_DIRECTORY", Path(directory.name)).start()
		self.journal = patch(f"{AGENT}.subprocess.run").start()
		self.addCleanup(patch.stopall)
		self.unit = ReleaseUnit("release-build-abc.service", "running", "success")

	def write_journal(self, output: str) -> None:
		self.journal.return_value = Mock(stdout=output)

	def test_the_first_report_reads_the_whole_journal_and_a_later_one_reads_after_its_cursor(self):
		self.write_journal("one\ntwo\n-- cursor: c1\n")
		self.assertEqual(build_agent.get_new_log(self.unit), ("one\ntwo\n", "c1"))
		self.assertNotIn("--after-cursor", self.journal.call_args.args[0])

		self.unit.cursor_file.write_text("c1")
		build_agent.get_new_log(self.unit)
		self.assertIn("c1", self.journal.call_args.args[0])

	def test_no_new_lines_keeps_the_cursor(self):
		self.write_journal("")
		self.assertEqual(build_agent.get_new_log(self.unit), ("", None))

	def test_the_cursor_moves_only_once_central_accepted_the_lines(self):
		self.write_journal("one\n-- cursor: c1\n")
		with (
			patch(f"{AGENT}.call", side_effect=URLError("down")),
			self.assertRaises(URLError),
		):
			build_agent.check_release(self.unit)
		self.assertFalse(self.unit.cursor_file.exists())

		with patch(f"{AGENT}.call") as call:
			build_agent.check_release(self.unit)
		self.assertEqual(call.call_args.kwargs["logs"], "one\n")
		self.assertEqual(self.unit.cursor_file.read_text(), "c1")

	def test_a_burst_of_output_sends_its_newest_part(self):
		self.write_journal("x" * (build_agent.MAX_REPORT_CHARACTERS + 10) + "end\n-- cursor: c1\n")
		log, _cursor = build_agent.get_new_log(self.unit)
		self.assertTrue(log.startswith("... earlier output skipped ...\n"))
		self.assertTrue(log.endswith("end\n"))


class UnitTestBuildAgentRelease(UnitTestCase):
	"""A release names its systemd unit after the deploy request Central sends."""

	def test_a_deploy_request_named_by_app_and_commit_is_accepted(self):
		for name in ("78n2bfuumh-5b0e33", "78n2bfuumh-5b0e33-1"):
			with self.subTest(name):
				self.assertEqual(Release(name, "a" * 40).unit, f"release-build-{name}.service")

	def test_a_deploy_request_that_could_escape_its_unit_name_is_refused(self):
		for name in ("", "-app", "app-", "app--1", "app/1", "app.1", "App-1", "app 1"):
			with self.subTest(name), self.assertRaises(ValueError):
				Release(name, "a" * 40)


class UnitTestBuildAgentImages(UnitTestCase):
	"""The agent keeps the newest and the live app image, and prunes only once Central has the outcome."""

	def setUp(self):
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		patch(f"{AGENT}.models.SECRETS_DIRECTORY", Path(directory.name)).start()
		self.addCleanup(patch.stopall)

	def test_old_app_images_are_removed_except_the_newest_and_the_live_one(self):
		images = [
			{"id": "live", "names": ["localhost/app:a"], "created": 1},
			{"id": "newest", "names": ["localhost/app:c"], "created": 3},
			{"id": "old", "names": ["localhost/app:b"], "created": 2},
			{"id": "base", "names": ["docker.io/library/python:3"], "created": 4},
			{"id": "dangling", "names": None, "created": 5},
		]
		run = patch(f"{AGENT}.subprocess.run", return_value=Mock(stdout=json.dumps(images))).start()
		patch(f"{AGENT}.get_live_slot", return_value="blue").start()
		patch(f"{AGENT}.get_slot_value", return_value="localhost/app:a").start()

		build_agent.prune_images()
		self.assertIn(call(["buildah", "rmi", "old"], check=True), run.call_args_list)

	def test_a_failed_prune_never_loses_a_build_or_its_release_report(self):
		patch(f"{AGENT}.subprocess.run").start()
		patch(f"{AGENT}.get_new_log", return_value=("", None)).start()
		report = patch(f"{AGENT}.call", return_value={"cancel": False}).start()
		patch(f"{AGENT}.read_environment", return_value={}).start()
		release = patch(f"{AGENT}.Release.from_environment").start().return_value
		prune = patch(f"{AGENT}.prune_images", side_effect=ValueError("bad image list")).start()

		self.assertEqual(
			build_agent.check_build(BuildUnit("deploy-build-abc.service", "exited", "success")), release
		)
		prune.assert_not_called()

		with self.assertRaises(ValueError):
			build_agent.check_release(ReleaseUnit("release-build-abc.service", "exited", "success"))
		self.assertEqual(report.call_args.args[0], "report_deployment_status")

	def test_a_failed_or_cancelled_build_is_pruned_once_reported(self):
		patch(f"{AGENT}.subprocess.run").start()
		patch(f"{AGENT}.get_new_log", return_value=("", None)).start()
		report = patch(f"{AGENT}.call").start()
		prune = patch(f"{AGENT}.prune_images").start()

		cases = (
			(BuildUnit("deploy-build-abc.service", "failed", "exit-code"), {"cancel": False}),
			(BuildUnit("deploy-build-abc.service", "exited", "success"), {"cancel": True}),
		)
		for unit, reply in cases:
			with self.subTest(unit.status, cancel=reply["cancel"]):
				prune.reset_mock()
				report.return_value = reply
				self.assertIsNone(build_agent.check_build(unit))
				prune.assert_called_once()
