# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from cargo.atlas_client import AtlasClient, AtlasNotFound
from cargo.cargo.doctype.machine.machine import Machine as MachineDoc


class PilotImageSnapshot(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from cargo.image_builder.doctype.pilot_image_snapshot_app.pilot_image_snapshot_app import (
			PilotImageSnapshotApp,
		)

		app: DF.Data | None
		apps: DF.Table[PilotImageSnapshotApp]
		built_at: DF.Datetime | None
		error: DF.LongText | None
		pilot_image: DF.Link
		snapshot_id: DF.Data | None
		status: DF.Literal["Pending", "Snapshotting", "Available", "Failed"]
	# end: auto-generated types

	"""One Atlas image taken off a Pilot Image build, with the apps it has turned on."""
