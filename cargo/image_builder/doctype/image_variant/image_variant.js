// Copyright (c) 2026, Aradhya-Tripathi and contributors
// For license information, please see license.txt

frappe.ui.form.on("Image Variant", {
	refresh(frm) {
		if (frm.is_new()) return;

		// The build writes its log as it runs, so follow it rather than making the operator
		// reload. frappe.realtime.off first, or a re-render subscribes twice.
		frappe.realtime.off("ssh_output");
		frappe.realtime.on("ssh_output", ({ name, fieldname, value }) => {
			if (name !== frm.doc.name) return;
			frm.doc[fieldname] = value;
			frm.refresh_field(fieldname);
			scroll_to_latest(frm, fieldname);
		});

		// While a build runs its log is in the cache, not the row, so a reload has to ask.
		if (["Building", "Snapshotting"].includes(frm.doc.status)) {
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
		} else {
			scroll_to_latest(frm, "build_log");
		}

		if (["Draft", "Failed", "Available"].includes(frm.doc.status)) {
			const first = frm.doc.status !== "Available";
			frm.add_custom_button(first ? __("Build") : __("Build Again"), () => {
				const flavour = [frm.doc.frappe_version, frm.doc.site].filter(Boolean).join(", ");
				frappe.confirm(
					first
						? __(
								"Rent a machine from Atlas, bake variant on it, snapshot it, then destroy it. This takes a few minutes.",
								[flavour || frm.doc.image]
						  )
						: __(
								"This image already has a snapshot. Building again replaces it with a new one; the old snapshot is left at Atlas."
						  ),
					() =>
						frm.call("build").then(() => {
							frappe.show_alert({
								message: __("Machine requested. It is baked once it boots."),
								indicator: "green",
							});
							frm.reload_doc();
						})
				);
			}).addClass(first ? "btn-primary" : "");
		}

		const headlines = {
			Draft: __("Nothing built yet."),
			Provisioning: __("Waiting for the build machine to boot."),
			Building: __("Installing on the build machine."),
			Snapshotting: __("Installed. Taking the snapshot, then destroying the machine."),
			Available: __("Snapshot {0} is ready for Atlas to boot.", [frm.doc.snapshot_id]),
			Failed: __("The last build failed. See Error below."),
		};
		if (headlines[frm.doc.status]) frm.dashboard.set_headline(headlines[frm.doc.status]);

		if (frm.doc.temporary_vm_id) {
			frm.dashboard.add_indicator(
				__("Build machine: {0}", [frm.doc.temporary_vm_id]),
				"orange"
			);
		}
	},
});

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
