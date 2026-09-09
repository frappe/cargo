// Copyright (c) 2026, Aradhya-Tripathi and contributors
// For license information, please see license.txt

const HEADLINES = {
	Draft: __("Add the telemetry node. Atlas builds it, and setting up starts once it boots."),
	"Setting Up": __(
		"Cloning datum, migrating ClickHouse and starting the service. Follow the Setup Log below."
	),
	Active: __(
		"Datum is running. Pilots ship metrics and logs to it once Central hands out its URL."
	),
	Failed: __(
		"The last run failed. See the Setup Log below, then set up again — the machine is kept, so this retries on it."
	),
};

frappe.ui.form.on("Datum Server", {
	refresh(frm) {
		if (frm.is_new()) return;

		follow_setup_log(frm);
		add_machine_button(frm);
		add_setup_button(frm);
		set_headline(frm);
	},
});

function set_headline(frm) {
	const guidance = HEADLINES[frm.doc.status];
	if (guidance) frm.dashboard.set_headline(guidance);
}

// One machine to a datum host, so this is offered until there is one and never again --
// replacing it means releasing the machine first.
function add_machine_button(frm) {
	if (frm.doc.machine || frm.doc.status === "Setting Up") return;

	frm.add_custom_button(__("Telemetry Node"), () => ask_for_machine(frm), __("Add")).addClass(
		"btn-primary"
	);
}

// One machine runs datum and ClickHouse's client load; the disk is what holds the checkout
// and datum's own logs, not the samples -- those live in ClickHouse.
function ask_for_machine(frm) {
	frappe.prompt(
		[
			{ fieldname: "cpu", label: __("vCPUs"), fieldtype: "Int", default: 2, reqd: 1 },
			{ fieldname: "ram_gb", label: __("RAM (GB)"), fieldtype: "Int", default: 4, reqd: 1 },
			{
				fieldname: "disk_gb",
				label: __("Disk (GB)"),
				description: __(
					"Holds the datum checkout and its logs. Samples go to ClickHouse, so this does not grow with traffic."
				),
				fieldtype: "Int",
				default: 20,
				reqd: 1,
			},
		],
		(values) =>
			frm.call("create_telemetry_node", values).then(() => {
				frappe.show_alert({
					message: __("Asked Atlas for the machine. It is ready once it boots."),
					indicator: "blue",
				});
				frm.reload_doc();
			}),
		__("Add Telemetry Node"),
		__("Ask Atlas")
	);
}

function add_setup_button(frm) {
	if (!frm.doc.machine || frm.doc.status === "Setting Up") return;

	// Idempotent: the install re-runs on whatever is already there, so it doubles as retry.
	const first = frm.doc.status !== "Active" && frm.doc.status !== "Failed";
	frm.add_custom_button(first ? __("Set Up Datum") : __("Set Up Again"), () => {
		frappe.confirm(
			first
				? __("Install datum on this machine?")
				: __(
						"Run the install again? It checks out {0} afresh, re-runs the migration and restarts the service.",
						[frm.doc.version || __("the configured version")]
				  ),
			() =>
				frm.call("setup").then(() => {
					frappe.show_alert({
						message: __("Setting up. This takes a few minutes."),
						indicator: "green",
					});
					frm.reload_doc();
				})
		);
	}).addClass(first ? "btn-primary" : "");
}

// Setup writes its log as it runs, so follow it rather than making the operator reload.
function follow_setup_log(frm) {
	// frappe.realtime.off first, or a re-render subscribes twice.
	frappe.realtime.off("ssh_output");
	frappe.realtime.on("ssh_output", ({ name, fieldname, value }) => {
		if (name !== frm.doc.name) return;
		frm.doc[fieldname] = value;
		frm.refresh_field(fieldname);
		scroll_to_latest(frm, fieldname);
	});

	// While setup runs its log is in the cache, not the row, so a reload has to ask.
	if (frm.doc.status !== "Setting Up") {
		scroll_to_latest(frm, "setup_log");
		return;
	}

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
}

// A capped log box is only useful if it shows the end of the log. The Code control loads
// ace lazily and sets its value inside that promise, so scrolling has to queue behind it.
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
