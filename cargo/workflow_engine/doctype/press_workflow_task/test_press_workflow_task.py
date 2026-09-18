# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.workflow_engine.doctype.press_workflow_task.press_workflow_task import retry_tasks

ENQUEUE = "cargo.workflow_engine.doctype.press_workflow_task.press_workflow_task.enqueue_task"


class IntegrationTestPressWorkflowTask(IntegrationTestCase):
	"""Picking up work a restarted worker dropped."""

	def setUp(self):
		frappe.set_user("Administrator")

	def workflow(self) -> str:
		"""A workflow waiting on a task. `linked_docname` is a real row, as the engine
		resolves it before running anything."""
		image = frappe.get_doc(
			{
				"doctype": "Pilot Image",
				"pilot_version": f"v0.0.1-{frappe.generate_hash(length=6)}",
				"frappe_version": "version-16",
				"has_site": 1,
			}
		).insert(ignore_permissions=True)

		with patch("cargo.workflow_engine.doctype.press_workflow.press_workflow.enqueue_workflow"):
			return (
				frappe.get_doc(
					{
						"doctype": "Press Workflow",
						"linked_doctype": image.doctype,
						"linked_docname": image.name,
						"main_method_name": "run_build",
						"main_method_title": "Run Build",
						"status": "Running",
					}
				)
				.insert(ignore_permissions=True)
				.name
			)

	def task(self, status: str) -> str:
		"""A task row in one state, without running it."""
		with patch(ENQUEUE):
			return (
				frappe.get_doc(
					{
						"doctype": "Press Workflow Task",
						"workflow": self.workflow(),
						"method_name": "run_provision_script",
						"method_title": "Run Provision Script",
						"signature": frappe.generate_hash(length=16),
						"status": status,
					}
				)
				.insert(ignore_permissions=True)
				.name
			)

	def test_a_task_a_restarted_worker_dropped_is_enqueued_again(self):
		"""Its job died with the worker, so nothing else will ever run it. The workflow
		waiting on it stays Running until this puts it back."""
		name = self.task("Running")

		with patch(ENQUEUE) as enqueue:
			retry_tasks()

		self.assertIn(name, [call.args[0] for call in enqueue.call_args_list])

	def test_a_task_still_waiting_for_a_worker_is_enqueued_again(self):
		"""Queued with no job behind it is the same problem, one step earlier."""
		name = self.task("Queued")

		with patch(ENQUEUE) as enqueue:
			retry_tasks()

		self.assertIn(name, [call.args[0] for call in enqueue.call_args_list])

	def test_a_finished_task_is_left_alone(self):
		"""Re-running one would repeat whatever it already did."""
		done = self.task("Success")
		failed = self.task("Failure")

		with patch(ENQUEUE) as enqueue:
			retry_tasks()

		asked = [call.args[0] for call in enqueue.call_args_list]
		self.assertNotIn(done, asked)
		self.assertNotIn(failed, asked)
