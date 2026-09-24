// Copyright (c) 2026, Aradhya-Tripathi and contributors
// For license information, please see license.txt

frappe.ui.form.on("Pilot Image Snapshot", {
	refresh(frm) {
		const headlines = {
			Pending: __("Waiting for the build to reach this snapshot."),
			Snapshotting: __(
				"Turning on only this snapshot's apps, then photographing the machine."
			),
			Available: __("Atlas image {0} is ready to boot.", [frm.doc.snapshot_id]),
			Failed: __("This snapshot was not taken. See Error below."),
		};
		frm.dashboard.set_headline(headlines[frm.doc.status]);
	},
});
