// Copyright (c) 2026, Aradhya-Tripathi and contributors
// For license information, please see license.txt

frappe.ui.form.on("Pilot Image", {
	refresh(frm) {
		if (frm.is_new()) return;

		follow_build_log(frm);
		set_headline(frm);
		add_actions(frm);

		const image_types = {
			Base: __("Bench only, no site"),
			Site: __("Bare site, every signup app fetched"),
			Apps: __("Site with one snapshot per signup app"),
		};
		frm.dashboard.add_indicator(image_types[frm.doc.image_type], "blue");
		if (frm.doc.frappe_version) {
			frm.dashboard.add_indicator(__("Frappe {0}", [frm.doc.frappe_version]), "gray");
		}
	},
});

function add_actions(frm) {
	if (["Provisioning", "Building", "Snapshotting"].includes(frm.doc.status)) {
		frm.add_custom_button(__("Stop Build"), () => {
			frappe.confirm(
				__(
					"Stop this build? It fails at its next step: its snapshots fail, their Atlas images are deleted and the build machine is released."
				),
				() =>
					frm
						.call({ doc: frm.doc, method: "stop_build", freeze: true })
						.then(() => frm.reload_doc())
			);
		});
	}

	if (frm.doc.status === "Failed") {
		frm.add_custom_button(__("Restart Build"), () => {
			frappe.confirm(
				__(
					"Delete this build's snapshots and their Atlas images, then build again from the start on a new machine?"
				),
				() =>
					frm
						.call({ doc: frm.doc, method: "restart_build", freeze: true })
						.then(() => frm.reload_doc())
			);
		});

		frm.add_custom_button(__("Set Automatic Retries"), () => {
			frappe.prompt(
				{
					fieldname: "count",
					fieldtype: "Int",
					label: __("Automatic Retries Used"),
					description: __(
						"The scheduler restarts this image while this is below Max Automatic Retries in Cargo Settings."
					),
					default: frm.doc.auto_retry_count,
					reqd: 1,
				},
				({ count }) =>
					frm
						.call({
							doc: frm.doc,
							method: "set_auto_retry_count",
							args: { count },
							freeze: true,
						})
						.then(() => frm.reload_doc()),
				__("Set Automatic Retries")
			);
		});
	}
}

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
		Building: __("Provisioning the build machine. The log below follows it."),
		Completed: __("Every snapshot is available at Atlas. The build machine is released."),
		Retired: __("A newer Pilot release replaced this one. Its Atlas images are deleted."),
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
				__("Taking snapshots: {0} of {1} available at Atlas.", [taken, snapshots.length])
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
