from __future__ import annotations

import frappe
from frappe import _

from cargo.central_client import CentralClient
from cargo.workflow_engine.doctype.press_workflow.decorators import flow, task
from cargo.workflow_engine.doctype.press_workflow.workflow_builder import WorkflowBuilder


class ServiceCluster(WorkflowBuilder):
	"""Base for a service's cluster doctype: the three setup stages every service shares."""

	SETUP_FROM = ("Credentials Minted", "Failed", "Active")
	#: Where a run's output is streamed. None for a service that keeps no log.
	SETUP_LOG = "setup_log"
	REQUIRED_FIELDS = ("status", "error", "region")
	REQUIRED_STATUSES = ("Setting Up", "Active", "Failed")

	@frappe.whitelist()
	def setup_cluster(self) -> str:
		self.check_contract()
		if self.status not in self.SETUP_FROM:
			frappe.throw(_(f"This cluster is {self.status}, not ready to set up."))

		# Here rather than only where the log is written: the run clears it when it starts,
		# which leaves the last run's output on the form for as long as the job sits queued.
		if self.SETUP_LOG:
			self.set(self.SETUP_LOG, None)

		self.mark("Setting Up")

		return self.run_setup.run_as_workflow()

	def check_contract(self) -> None:
		"""What this base touches on a subclass's doctype. Checked before the first stage
		runs, so a new service hears about a missing field here and not three stages in."""
		fields = (*self.REQUIRED_FIELDS, *filter(None, (self.SETUP_LOG,)))
		missing = [field for field in fields if not self.meta.has_field(field)]

		status = self.meta.get_field("status")
		options = (status.options or "").split("\n") if status else []
		missing += [
			f"the status option {name}"
			for name in dict.fromkeys((*self.SETUP_FROM, *self.REQUIRED_STATUSES))
			if name not in options
		]

		if missing:
			frappe.throw(_("{0} is missing {1}.").format(self.doctype, ", ".join(missing)))

	@flow
	def run_setup(self) -> None:
		"""Bring the cluster up"""
		self.terraform()
		self.verify()
		self.register()

	@task
	def terraform(self) -> None:
		"""Configure the machines"""
		raise NotImplementedError

	@task
	def verify(self) -> None:
		"""Check the cluster works"""
		raise NotImplementedError

	@task
	def register(self) -> None:
		"""Hand the cluster to Central. Last, so Central only ever points at a working one."""
		raise NotImplementedError

	def on_workflow_success(self, workflow) -> None:
		self.mark("Active")

	def on_workflow_failure(self, workflow) -> None:
		"""Record which stage failed, and tell Central its secrets went nowhere."""
		failed = next((row for row in workflow.steps if row.status == "Failure"), None)
		stage = failed.step_title if failed else "Setup"
		reason = frappe.db.get_value("Press Workflow Task", failed.task, "traceback") if failed else None
		error = (reason or workflow.workflow_traceback or "").strip() or "failed"

		self.mark("Failed", error=f"{stage}\n{error}")
		self.report_failure(stage, error)

	def mark(self, status: str, error: str | None = None) -> None:
		self.status = status
		self.error = error
		self.save(ignore_permissions=True)

	def report_failure(self, stage: str, error: str) -> None:
		"""Best effort: the cluster is already Failed, and Central being unreachable must
		not replace that with a less useful error."""
		try:
			CentralClient.from_settings().report_failure(self.region, stage, error)
		except Exception:
			frappe.log_error(title=f"Could not report {self.name} failure to Central")
