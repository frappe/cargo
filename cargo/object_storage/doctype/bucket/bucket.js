// Copyright (c) 2026, Aradhya-Tripathi and contributors
// For license information, please see license.txt

frappe.ui.form.on("Bucket", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Show Usage"), () => show_usage(frm));

		// Central issues and holds these keys. Rotating or revoking one here leaves Central
		// and the bench that uses it holding a key that opens nothing, so both are last
		// resorts rather than routine.
		frm.add_custom_button(
			__("Rotate Credentials"),
			() =>
				frappe.confirm(
					__(
						"Central will still hold the old key. Every client using it stops working. Continue?"
					),
					() => frm.call("rotate_credentials").then(() => frm.reload_doc())
				),
			__("Emergency")
		);

		if (frm.doc.access_key) {
			frm.add_custom_button(
				__("Revoke Credentials"),
				() =>
					frappe.confirm(
						__(
							"This bucket becomes unreachable until a key is issued again. Continue?"
						),
						() => frm.call("revoke_credentials").then(() => frm.reload_doc())
					),
				__("Emergency")
			);
		}
	},
});

function show_usage(frm) {
	frm.call("get_usage").then(({ message }) => {
		const cap = (value) => (value ? value : __("Uncapped"));
		frappe.msgprint({
			title: __("Usage"),
			message: `
				<p>${__("Size")}: ${frappe.format(message.used_bytes, { fieldtype: "Int" })} ${__(
				"bytes"
			)} / ${cap(message.quota_bytes)}</p>
				<p>${__("Objects")}: ${message.object_count} / ${cap(message.quota_objects)}</p>
			`,
		});
	});
}
