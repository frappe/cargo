// Copyright (c) 2026, Aradhya-Tripathi and contributors
// For license information, please see license.txt

frappe.ui.form.on("Cargo Settings", {
	refresh(frm) {
		frm.add_custom_button(
			__("Base Images"),
			() => {
				frappe.confirm(
					__(
						"Build a Base image of the newest Pilot release on the head commit of each Frappe branch? Branches that already have one are skipped."
					),
					() =>
						frm
							.call({ doc: frm.doc, method: "build_base_images", freeze: true })
							.then(({ message }) =>
								frappe.show_alert(__("Started {0} Base images", [message.length]))
							)
				);
			},
			__("Build")
		);
	},
});
