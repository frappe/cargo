// Copyright (c) 2026, Aradhya-Tripathi and contributors
// For license information, please see license.txt

frappe.ui.form.on("Pilot Image", {
	refresh(frm) {
		if (frm.is_new()) return;

		follow_build_log(frm);
		set_headline(frm);

		frm.dashboard.add_indicator(
			frm.doc.has_site ? __("Site baked in") : __("No site baked in"),
			frm.doc.has_site ? "green" : "blue"
		);
		if (frm.doc.has_apps) {
			frm.dashboard.add_indicator(__("One snapshot per signup app"), "blue");
		}
	},
});

// The build writes its log as it runs, so follow it rather than making the operator reload.
function follow_build_log(frm) {
	// frappe.realtime.off first, or a re-render subscribes twice.
	frappe.realtime.off("ssh_output");
	frappe.realtime.on("ssh_output", ({ name, fieldname, value }) => {
		if (name !== frm.doc.name) return;
		frm.doc[fieldname] = value;
		frm.refresh_field(fieldname);
		scroll_to_latest(frm, fieldname);
	});

	// While the script runs its log is in the cache, not the row, so a reload has to ask.
	if (frm.doc.status !== "Building") {
		scroll_to_latest(frm, "build_log");
		return;
	}

	frappe
		.xcall("cargo.ssh.get_live_output", {
			doctype: frm.doctype,
			name: frm.doc.name,
			fieldname: "build_log",
		})
		.then((value) => {
			if (!value) return;
			frm.doc.build_log = value;
			frm.refresh_field("build_log");
			scroll_to_latest(frm, "build_log");
		});
}

function set_headline(frm) {
	const headlines = {
		Provisioning: __("Waiting for build machine {0} to boot.", [frm.doc.machine]),
		Building: __("Installing on the build machine. The log below follows it."),
		Completed: __("Every snapshot is at Atlas. The build machine is released."),
		Failed: __("The build failed and its machine is released. See Error below."),
	};
	if (frm.doc.status !== "Snapshotting") {
		frm.dashboard.set_headline(headlines[frm.doc.status]);
		return;
	}

	frappe.db
		.get_list("Pilot Image Snapshot", {
			filters: { pilot_image: frm.doc.name },
			fields: ["status"],
			limit: 0,
		})
		.then((snapshots) => {
			const taken = snapshots.filter((snapshot) => snapshot.status === "Available").length;
			frm.dashboard.set_headline(
				__("Taking snapshots: {0} of {1} at Atlas.", [taken, snapshots.length])
			);
		});
}

// A capped log box is only useful if it shows the end of the log. The Code control loads
// ace lazily and sets its value inside that promise, so scrolling has to queue behind it --
// scrolling straight after refresh_field runs before the new text is in the editor.
function scroll_to_latest(frm, fieldname) {
	const field = frm.get_field(fieldname);
	if (!field?.load_lib) return;

	field.load_lib().then(() => {
		const editor = field.editor;
		if (!editor) return;
		const last_line = editor.session.getLength();
		editor.navigateFileEnd();
		editor.renderer.scrollToRow(last_line);
	});
}
