from __future__ import annotations

import frappe
from frappe import _

from cargo.central_client import CentralClient
from cargo.workflow_engine.doctype.press_workflow.decorators import flow, task
from cargo.workflow_engine.doctype.press_workflow.workflow_builder import WorkflowBuilder


class ServiceCluster(WorkflowBuilder):
	"""Base for a service's cluster doctype.

	Setting one up is the same three stages for every service, so the order and the
	failure handling live here and each service fills in the stages.

	Machines are provisioned before this runs: booting takes minutes and is driven by the
	scheduler, not by a worker holding a job open.
	"""

	SETUP_FROM = ("Credentials Minted", "Failed", "Active")

	@frappe.whitelist()
	def setup_cluster(self) -> str:
		if self.status not in self.SETUP_FROM:
			frappe.throw(_(f"This cluster is {self.status}, not ready to set up."))

		return self.run_setup.run_as_workflow()

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
		"""Hand the cluster to Central

		Last, so Central only ever points at a cluster that has been proven to work."""
		raise NotImplementedError

	def on_workflow_success(self, workflow) -> None:
		self.mark("Active")

	def on_workflow_failure(self, workflow) -> None:
		"""Record which stage failed, and tell Central its secrets went nowhere."""
		failed = next((row for row in workflow.steps if row.status == "Failure"), None)
		stage = failed.step_title if failed else "Setup"
		reason = frappe.db.get_value("Press Workflow Task", failed.task, "traceback") if failed else None
		error = (reason or workflow.workflow_traceback or "").strip().splitlines()[-1:] or ["failed"]

		self.mark("Failed", error=f"{stage}: {error[0]}"[:400])
		self.report_failure(stage, error[0])

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
