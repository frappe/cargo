import frappe
from frappe import _
from frappe.utils import now_datetime

from cargo.atlas_client import AtlasClient, AtlasNotFound
from cargo.cargo.doctype.machine.machine import DEAD_MACHINE_STATES, Machine
from cargo.image_builder.doctype.pilot_image.builder import Builder, get_build_spec


class BuildMachineOwner:
	"""An image doctype that rents one build machine, builds on it and snapshots it. It needs
	`status`, `error`, `machine`, `snapshot_id` and `built_at` fields, plus `get_boot_image()`,
	`get_atlas_tags()`, `is_warm_image` and a `create_image()` that runs its workflow.

	`take_snapshot` and `wait_for_snapshot` are plain here: the workflow only tracks tasks a
	class declares itself, so each image declares them as `@task` and calls `super()`."""

	is_warm_image = False
	has_ssh_after_build = True

	@property
	def build_machine(self) -> Machine | None:
		if not self.machine:
			return None

		return frappe.get_doc("Machine", self.machine)

	def request_build_machine(self) -> None:
		"""Rent a machine to build on. `sync_machines` starts the build once it is running."""
		machine = Machine.request(owner=self, spec=get_build_spec(), base_image=self.get_boot_image()).name
		self.db_set({"machine": machine, "status": "Provisioning"})

	def sync_machines(self) -> None:
		"""Once the machine is live we can start the initial build process."""
		# Past this, the workflow owns the machine: a finished build terminates it on purpose.
		if self.status != "Provisioning":
			return

		machine_status = frappe.db.get_value("Machine", self.machine, "status")
		if machine_status in DEAD_MACHINE_STATES:
			self.status = "Failed"
			self.error = f"Machine {self.machine} is dead. Status: {machine_status}"
			self.save()
			return

		if machine_status == "Running":
			self.status = "Building"
			self.save()
			self.create_image()
			return

		# Any thing other than Dead and Running is a transient state, wait for it.
		return

	def take_snapshot(self) -> None:
		"""Stop the build machine, then ask Atlas to capture its disk."""
		machine = self.build_machine
		Builder().stop_build_machine(machine, flush=self.has_ssh_after_build)

		self.snapshot_id = machine.snapshot(
			self.name,
			self.get_atlas_tags(),
			cache_image=self.is_warm_image,
			memory_snapshot=self.is_warm_image,
		)
		self.status = "Snapshotting"
		self.save()

	def wait_for_snapshot(self) -> None:
		"""Wait for Atlas to make the image. A deferred task is run again about once a minute."""
		atlas_image = AtlasClient.from_settings().get_snapshot(self.snapshot_id)
		status = atlas_image.get("status")
		if status in ("failed", "deleting", "archived"):
			frappe.throw(
				_("Atlas image {0} is {1}: {2}").format(
					self.snapshot_id, status, atlas_image.get("transfer_error") or _("Atlas gave no reason.")
				)
			)

		if status != "available":
			self.defer_current_task(_("Atlas is still making image {0}.").format(self.snapshot_id))
			return

		self.built_at = now_datetime()
		self.save()

	def on_workflow_failure(self, workflow) -> None:
		"""The workflow engine has given up on this image. The machine is still running, but
		the image is not going to be built."""
		failed = frappe.db.get_value(
			"Press Workflow Task",
			{"workflow": workflow.name, "status": "Failure"},
			["method_title", "traceback"],
			order_by="creation desc",
			as_dict=True,
		)
		stage = failed.method_title if failed else _("Build")
		if workflow.is_force_failure_requested:
			stage = _("Stopped by request during: {0}").format(stage)
		traceback = ((failed and failed.traceback) or workflow.workflow_traceback or "").strip()

		# Recorded before the machine goes, so a failed release cannot hide why the build failed.
		self.status = "Failed"
		self.error = f"{stage}\n{traceback}"
		self.save()

		self.release_build_machine()
		self.delete_atlas_image()

	def on_workflow_success(self, workflow) -> None:
		"""Record the completed image and release its stopped build machine."""
		self.status = "Completed"
		self.error = None
		self.save()

		self.release_build_machine()

	def release_build_machine(self) -> None:
		"""Let the build machine go. A refusal leaves it Broken and in the Error Log, which
		`Machine.terminate` records."""
		machine = self.build_machine
		if machine and machine.status != "Terminated":
			machine.terminate()

	def delete_atlas_image(self) -> None:
		"""Delete the Atlas image this build took, so it cannot be booted. Throws while Atlas
		still has it, so the caller can try again."""
		if not self.snapshot_id:
			return

		client = AtlasClient.from_settings()
		try:
			# The snapshot is a System image, which Atlas protects from deletion.
			client.set_image_termination_protection(self.snapshot_id, enabled=False)
			client.delete_snapshot(self.snapshot_id)
			status = client.get_snapshot(self.snapshot_id).get("status")
		except AtlasNotFound:
			status = "deleted"

		# Atlas reclaims a deleting or archived image itself, and boots neither.
		if status not in ("deleted", "deleting", "archived"):
			frappe.throw(_("Atlas still has image {0}, which is {1}.").format(self.snapshot_id, status))

		self.db_set("snapshot_id", None)
