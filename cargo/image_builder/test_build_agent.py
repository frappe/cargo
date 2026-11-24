# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import URLError

from frappe.tests import UnitTestCase

from cargo.image_builder.conf import build_agent
from cargo.image_builder.conf.build_agent.models import ReleaseUnit

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
