// Copyright (c) 2026, Aradhya-Tripathi and contributors
// For license information, please see license.txt

frappe.ui.form.on("Bucket", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Show Usage"), () => show_usage(frm));

		frm.add_custom_button(
			__("Add Credentials"),
			() =>
				frappe.confirm(
					__("Issue one more key for this bucket? Its other keys keep working."),
					() => frm.call("add_credentials").then(() => frm.reload_doc())
				),
			__("Credentials")
		);

		if (!frm.doc.bucket_credentials?.length) return;

		// Central holds these keys, so rotating or removing one here breaks its clients.
		frm.add_custom_button(
			__("Rotate Credentials"),
			() =>
				pick_key(
					frm,
					__("Rotate Credentials"),
					__(
						"Central will still hold the old key. Every client using it stops working. Continue?"
					),
					"rotate_credentials"
				),
			__("Credentials")
		);

		if (frm.doc.bucket_credentials.length > 1) {
			frm.add_custom_button(
				__("Remove Credentials"),
				() =>
					pick_key(
						frm,
						__("Remove Credentials"),
						__("Every client using this key loses access to the bucket. Continue?"),
						"remove_credentials"
					),
				__("Credentials")
			);
		}
	},
});

function pick_key(frm, title, warning, method) {
	frappe.prompt(
		{
			fieldname: "access_key",
			fieldtype: "Select",
			label: __("Access Key"),
			options: frm.doc.bucket_credentials.map((credential) => credential.access_key),
			reqd: 1,
		},
		({ access_key }) =>
			frappe.confirm(warning, () =>
				frm.call(method, { access_key }).then(() => frm.reload_doc())
			),
		title
	);
}

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
