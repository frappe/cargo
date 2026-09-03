// Copyright (c) 2026, Aradhya-Tripathi and contributors
// For license information, please see license.txt

frappe.ui.form.on("Object Storage Cluster", {
	refresh(frm) {
		if (frm.is_new()) return;

		// Setup writes its log as it runs, so follow it rather than making the operator
		// reload. frappe.realtime.off first, or a re-render subscribes twice.
		frappe.realtime.off("ssh_output");
		frappe.realtime.on("ssh_output", ({ name, fieldname, value }) => {
			if (name !== frm.doc.name) return;
			frm.doc[fieldname] = value;
			frm.refresh_field(fieldname);
			scroll_to_latest(frm, fieldname);
		});

		// While setup runs its log is in the cache, not the row, so a reload has to ask.
		if (frm.doc.status === "Setting Up") {
			frappe
				.xcall("cargo.ssh.get_live_output", {
					doctype: frm.doctype,
					name: frm.doc.name,
					fieldname: "setup_log",
				})
				.then((value) => {
					if (!value) return;
					frm.doc.setup_log = value;
					frm.refresh_field("setup_log");
					scroll_to_latest(frm, "setup_log");
				});
		} else {
			scroll_to_latest(frm, "setup_log");
		}

		if (frm.doc.status === "Draft") {
			frm.add_custom_button(__("Provision"), () => {
				frappe.confirm(
					__("Ask Atlas for one gateway and {0} storage nodes?", [
						frm.doc.storage_count,
					]),
					() =>
						frm.call("provision").then(() => {
							frappe.show_alert({
								message: __("Requested. Watch the status here."),
								indicator: "blue",
							});
							frm.reload_doc();
						})
				);
			}).addClass("btn-primary");
		}

		if (["Machines Ready", "Minting Failed"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Mint Credentials"), () => {
				frappe.confirm(
					__(
						"Ask Central for this cluster's secrets? Asking again is safe: Central answers the same secrets for a region every time."
					),
					() =>
						frm.call("mint_credentials").then(() => {
							frm.reload_doc();
						})
				);
			}).addClass("btn-primary");
		}

		if (frm.doc.status === "Active") {
			frm.add_custom_button(__("Add Storage Nodes"), () => {
				frappe.prompt(
					{
						fieldname: "storage_node_increment",
						label: __("How many more storage nodes?"),
						fieldtype: "Int",
						default: 1,
						reqd: 1,
					},
					({ storage_node_increment }) =>
						frm.call("increase_storage_node", { storage_node_increment }).then(() => {
							frappe.show_alert({
								message: __("Requested. They join once they boot."),
								indicator: "blue",
							});
							frm.reload_doc();
						}),
					__("Add Storage Nodes"),
					__("Ask Atlas")
				);
			});
		}

		if (["Credentials Minted", "Failed", "Active"].includes(frm.doc.status)) {
			const first = frm.doc.status === "Credentials Minted";
			frm.add_custom_button(first ? __("Set Up Cluster") : __("Run Setup Again"), () => {
				const warning = __(
					"Setup runs from scratch every time: every node's config is rewritten and Garage is restarted, and the cluster gets a new layout version. Expect brief downtime while nodes come back."
				);
				frappe.confirm(
					first
						? __("Install Garage on {0} machines?", [frm.doc.storage_count + 1])
						: warning,
					() =>
						frm.call("setup_cluster").then(() => {
							frappe.show_alert({
								message: __("Setting up. This takes a few minutes."),
								indicator: "green",
							});
							frm.reload_doc();
						})
				);
			}).addClass(first ? "btn-primary" : "");
		}

		const headlines = {
			Pending: __("Waiting for machines to boot."),
			"Machines Ready": __("Machines are up. Asking Central for the cluster's secrets."),
			"Minting Failed": __(
				"Central would not issue the cluster's secrets. See Error below, then mint again."
			),
			"Credentials Minted": __("Secrets issued. Ready to install Garage."),
			"Setting Up": __("Installing Garage on the machines. Follow the Setup Log below."),
			"Increasing Storage Nodes": __("Waiting for the new storage machines to boot."),
			"Joining Storage Nodes": __("The new machines are up. Adding them to the cluster."),
			Active: __("Garage is running and Central can use this cluster."),
			Failed: __("The last run failed. See Error below."),
		};
		if (headlines[frm.doc.status]) frm.dashboard.set_headline(headlines[frm.doc.status]);
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
