// Copyright (c) 2026, Aradhya-Tripathi and contributors
// For license information, please see license.txt

frappe.ui.form.on("Machine", {
	refresh(frm) {
		if (!frm.doc.vm_id) return;

		frm.add_custom_button(__("Sync with Atlas"), () =>
			frm
				.call({
					method: "sync_now",
					doc: frm.doc,
					freeze: true,
					freeze_message: __("Asking Atlas..."),
				})
				.then(({ message }) => {
					frm.reload_doc();
					frappe.show_alert({
						message: __("Synced: {0}", [message]),
						indicator: "green",
					});
				})
		);
	},
});
