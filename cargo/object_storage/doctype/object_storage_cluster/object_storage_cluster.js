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

		// A cluster takes machines in every state but one: while it installs on what it has.
		if (frm.doc.status !== "Setting Up") {
			const spec = (label) => [
				{
					fieldname: "cpu",
					label: __("vCPUs"),
					fieldtype: "Int",
					default: 2,
					reqd: 1,
				},
				{
					fieldname: "ram_gb",
					label: __("RAM (GB)"),
					fieldtype: "Int",
					default: 4,
					reqd: 1,
				},
				{
					fieldname: "disk_gb",
					label: __("Disk (GB)"),
					description: label,
					fieldtype: "Int",
					default: 100,
					reqd: 1,
				},
			];

			const add = (method, values, message) =>
				frm.call(method, values).then(({ message: name }) => {
					frappe.show_alert({ message: __(message, [name]), indicator: "blue" });
					frm.reload_doc();
				});

			// One gateway to a cluster, so the button goes once it has one.
			if (!(frm.doc.machines || []).some((row) => row.role === "gateway")) {
				frm.add_custom_button(__("Add Gateway Node"), () => {
					frappe.prompt(
						spec(__("A gateway only passes traffic through, so it barely needs one.")),
						(values) => add("add_gateway_node", values, "Asked Atlas for {0}."),
						__("Add Gateway Node"),
						__("Ask Atlas")
					);
				}).addClass("btn-primary");
			}

			frm.add_custom_button(__("Add Storage Node"), () => {
				frappe.prompt(
					spec(
						__(
							"This node's own disk. Garage is not RAID 0, so it need not match the others."
						)
					),
					(values) => add("add_storage_node", values, "Asked Atlas for {0}."),
					__("Add Storage Node"),
					__("Ask Atlas")
				);
			});
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

		if (["Credentials Minted", "Failed", "Active"].includes(frm.doc.status)) {
			const first = frm.doc.status === "Credentials Minted";
			frm.add_custom_button(first ? __("Set Up Cluster") : __("Run Setup Again"), () => {
				const warning = __(
					"Setup runs from scratch every time: every node's config is rewritten and Garage is restarted, and the cluster gets a new layout version. Expect brief downtime while nodes come back."
				);
				frappe.confirm(
					first ? __("Install Garage on this cluster's machines?") : warning,
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
			Draft: __("Add a gateway and its storage nodes. Each one is asked for as you add it."),
			Pending: __("Waiting for machines to boot."),
			"Machines Ready": __("Machines are up. Asking Central for the cluster's secrets."),
			"Minting Failed": __(
				"Central would not issue the cluster's secrets. See Error below, then mint again."
			),
			"Credentials Minted": __("Secrets issued. Ready to install Garage."),
			"Setting Up": __("Installing Garage on the machines. Follow the Setup Log below."),
			Active: __("Garage is running and Central can use this cluster."),
			Failed: __("The last run failed. See Error below."),
		};
		if (headlines[frm.doc.status]) frm.dashboard.set_headline(headlines[frm.doc.status]);

		// Status is what Cargo is doing; health is what users get. A live cluster whose
		// machine died keeps serving, so it stays Active and says Degraded here.
		const colors = { Healthy: "green", Degraded: "orange", Critical: "red" };
		if (colors[frm.doc.health]) {
			frm.page.set_indicator(__(frm.doc.health), colors[frm.doc.health]);
		}
		if (frm.doc.health_reason) {
			frm.dashboard.set_headline(
				`${__(frm.doc.health)}: ${frm.doc.health_reason}`,
				colors[frm.doc.health]
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
