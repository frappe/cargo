// Copyright (c) 2026, Aradhya-Tripathi and contributors
// For license information, please see license.txt

frappe.ui.form.on("Object Storage Cluster", {
	refresh(frm) {
		if (frm.is_new()) return;

		if (frm.doc.status === "Draft") {
			frm.add_custom_button(__("Provision"), () => {
				frappe.confirm(
					__("Ask Atlas for one gateway and {0} storage nodes?", [
						frm.doc.storage_count,
					]),
					() =>
						frm.call("provision").then(() => {
							frappe.show_alert({
								message: __("Machines requested. They appear here as they boot."),
								indicator: "green",
							});
							frm.reload_doc();
						})
				);
			}).addClass("btn-primary");
		}

		if (frm.doc.status === "Credentials Minted") {
			frm.add_custom_button(__("Set Up Cluster"), () =>
				frm.call("setup_cluster").then(() => {
					frappe.show_alert({
						message: __("Installing Garage. This takes a few minutes."),
						indicator: "green",
					});
					frm.reload_doc();
				})
			).addClass("btn-primary");
		}

		const headlines = {
			Pending: __("Waiting for machines to boot."),
			"Machines Ready": __("Machines are up. Asking Central for the cluster's secrets."),
			"Credentials Minted": __("Secrets issued. Ready to install Garage."),
			Active: __("Garage is running and Central can use this cluster."),
		};
		if (headlines[frm.doc.status]) frm.dashboard.set_headline(headlines[frm.doc.status]);
	},
});
