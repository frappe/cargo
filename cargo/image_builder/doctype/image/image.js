// Copyright (c) 2026, Aradhya-Tripathi and contributors
// For license information, please see license.txt

frappe.ui.form.on("Image", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Generate Variants"), () => {
			frm.call("generate_variants").then(() => {
				frappe.show_alert({
					message: __("Variants generated."),
					indicator: "green",
				});
				frm.reload_doc();
			});
		}).addClass("btn-primary");
	},
});
