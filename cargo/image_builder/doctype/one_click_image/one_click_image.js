// Copyright (c) 2026, Aradhya-Tripathi and contributors
// For license information, please see license.txt

frappe.ui.form.on("One Click Image", {
	refresh(frm) {
		if (!["Completed", "Failed"].includes(frm.doc.status)) return;

		frm.add_custom_button(__("Retire"), () => {
			frappe.confirm(
				__("Retire this image? Its Atlas image is deleted and Central stops offering it."),
				() =>
					frm
						.call({ doc: frm.doc, method: "retire", freeze: true })
						.then(() => frm.reload_doc())
			);
		});
	},
});
