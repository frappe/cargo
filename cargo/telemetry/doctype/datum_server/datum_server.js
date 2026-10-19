// Copyright (c) 2026, Aradhya-Tripathi and contributors
// For license information, please see license.txt

frappe.ui.form.on("Datum Server", {
	refresh(frm) {
		if (frm.is_new()) return;

		follow_setup_log(frm);
		add_machine_button(frm);
		add_setup_button(frm);
		add_update_button(frm);
		add_reset_attempts_button(frm);
		add_password_buttons(frm);
		set_headline(frm);
	},
});

function set_headline(frm) {
	const headlines = {
		Draft: __("Add the telemetry node. Atlas builds it, and setting up starts once it boots."),
		"Setting Up": __(
			"Cloning datum, migrating ClickHouse and starting the service. Follow the Setup Log below."
		),
		Updating: __(
			"Pulling datum, migrating ClickHouse and restarting the service. Follow the Setup Log below."
		),
		Active: __(
			"Datum is running. Pilots ship metrics and logs to it once Central hands out its URL."
		),
		Unreachable: __(
			"Datum or ClickHouse stopped answering. See Error for which; this turns Active again by itself once both answer."
		),
		Failed: __(
			"The last run failed. See the Setup Log below, then set up again — the machine is kept, so this retries on it."
		),
	};

	const guidance = headlines[frm.doc.status];
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
			{
				fieldname: "cpu_millicores",
				label: __("CPU (millicores)"),
				description: __("1000 millicores equals one CPU core."),
				fieldtype: "Int",
				default: 2000,
				reqd: 1,
			},
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
	if (!frm.doc.machine || is_running(frm)) return;

	// Idempotent: the install re-runs on whatever is already there, so it doubles as retry.
	const first = !["Active", "Unreachable", "Failed"].includes(frm.doc.status);
	frm.add_custom_button(first ? __("Set Up Datum") : __("Set Up Again"), () => {
		frappe.confirm(
			first
				? __("Install datum on this machine?")
				: __(
						"Run the install again? It checks out {0} afresh, re-runs the migration and restarts the service.",
						[frm.doc.commit || frm.doc.branch]
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

// Only a host that is set up has a checkout and a service to move.
function add_update_button(frm) {
	if (!["Active", "Unreachable"].includes(frm.doc.status)) return;

	frm.add_custom_button(__("Update Datum"), () => {
		frappe.prompt(
			[
				{
					fieldname: "branch",
					label: __("Branch"),
					fieldtype: "Data",
					default: frm.doc.branch,
					reqd: 1,
				},
				{
					fieldname: "commit",
					label: __("Commit"),
					fieldtype: "Data",
					description: __("Leave blank for the newest commit of the branch."),
				},
			],
			({ branch, commit }) =>
				frm.call("update_datum", { branch, commit }).then(() => {
					frappe.show_alert({
						message: __("Updating. Datum restarts once it is pulled."),
						indicator: "green",
					});
					frm.reload_doc();
				}),
			__("Update Datum"),
			__("Update")
		);
	});
}

function is_running(frm) {
	return ["Setting Up", "Updating"].includes(frm.doc.status);
}

function add_reset_attempts_button(frm) {
	if (!frm.doc.auto_spawn || !frm.doc.auto_setup_attempts) return;

	frm.add_custom_button(__("Reset Setup Attempts"), () => {
		frappe.confirm(__("Let Cargo try setting up this host again by itself?"), () =>
			frm.call("reset_auto_setup_attempts").then(() => frm.reload_doc())
		);
	});
}

// Fetched only when asked for, never with the form.
function add_password_buttons(frm) {
	const users = [
		{
			fieldname: "default_user_password",
			label: __("Default User"),
			warning: __("ClickHouse superuser: every table, every user and every grant."),
		},
		{
			fieldname: "datum_user_password",
			label: __("Datum User"),
			warning: __(
				"Reads and inserts rows in the datum database. Datum's API connects as this user."
			),
		},
		{
			fieldname: "insights_user_password",
			label: __("Insights User"),
			warning: __("Reads the datum database, and can create and drop views in it."),
		},
	];

	for (const { fieldname, label, warning } of users) {
		frm.add_custom_button(
			label,
			() =>
				frm.call("reveal_password", { fieldname }).then(({ message: password }) => {
					const dialog = new frappe.ui.Dialog({
						title: __("{0} Password", [label]),
						fields: [
							{
								fieldtype: "HTML",
								options: `<p class="text-muted">${warning} ${__(
									"Do not paste it anywhere it will be kept."
								)}</p>`,
							},
							{
								fieldname: "password",
								fieldtype: "Code",
								read_only: 1,
								default: password,
							},
						],
						primary_action_label: __("Copy"),
						primary_action: () => {
							frappe.utils.copy_to_clipboard(password);
							dialog.hide();
						},
					});

					dialog.show();
				}),
			__("Show Password")
		);
	}
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

	// While setup or an update runs its log is in the cache, not the row, so a reload has to ask.
	if (!is_running(frm)) {
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
