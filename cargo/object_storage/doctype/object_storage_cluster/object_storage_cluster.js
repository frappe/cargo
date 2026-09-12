// Copyright (c) 2026, Aradhya-Tripathi and contributors
// For license information, please see license.txt

const HEALTH_COLORS = { Healthy: "green", Degraded: "orange", Critical: "red", Unknown: "gray" };

// Only Draft: the other three read the same whoever built the cluster.
const AUTO_HEADLINES = {
	Draft: __("Cargo is asking Atlas for this cluster's machines, and sets it up once they boot."),
};

const HEADLINES = {
	Draft: __("Add a gateway and its storage nodes. Each one is asked for as you add it."),
	"Setting Up": __("Installing Garage on the machines. Follow the Setup Log below."),
	Active: __("Garage is running and the nodes carry their layout. This cluster can serve."),
	Failed: __(
		"The last run failed. See Error below, then set up again — the machines are kept, so setting up again retries them."
	),
};

frappe.ui.form.on("Object Storage Cluster", {
	refresh(frm) {
		if (frm.is_new()) return;

		follow_setup_log(frm);
		add_machine_buttons(frm);
		add_cluster_buttons(frm);
		add_admin_token_button(frm);

		set_headline(frm);
	},
});

// One line, not two indicators: the page indicator is Frappe's, driven by status -- what
// Cargo is doing. Health is what users get from the cluster, so it reads as a sentence
// underneath rather than as a second, competing status pill.
function set_headline(frm) {
	// A cluster Cargo made is not one the operator adds machines to, so it reads differently.
	const guidance =
		(frm.doc.auto_spawn && AUTO_HEADLINES[frm.doc.status]) || HEADLINES[frm.doc.status];
	const health = frm.doc.health;
	if (!HEALTH_COLORS[health] || health === "Unknown") {
		if (guidance) frm.dashboard.set_headline(guidance);
		return;
	}

	const reason = frm.doc.health_reason ? ` — ${frm.doc.health_reason}` : "";
	frm.dashboard.set_headline(
		`<b>${__("Health")}: ${__(health)}</b>${frappe.utils.escape_html(reason)}<br>${
			guidance || ""
		}`,
		HEALTH_COLORS[health]
	);
}

// Machines are asked for one at a time, so both buttons stay available for as long as the
// cluster is not installing on the machines it already has.
function add_machine_buttons(frm) {
	if (frm.doc.status === "Setting Up") return;

	const machines = frm.doc.machines || [];
	if (!machines.some((row) => row.role === "gateway")) {
		frm.add_custom_button(
			__("Gateway Node"),
			() =>
				ask_for_machine(
					frm,
					"add_gateway_node",
					__("Add Gateway Node"),
					__("Every S3 request reaches the cluster through this one machine."),
					20
				),
			__("Add")
		);
	}

	frm.add_custom_button(
		__("Storage Node"),
		() =>
			ask_for_machine(
				frm,
				"add_storage_node",
				__("Add Storage Node"),
				__("This node's own disk. Garage is not RAID 0, so it need not match the others."),
				100
			),
		__("Add")
	);
}

function add_cluster_buttons(frm) {
	if (frm.doc.status === "Setting Up") return;

	// Idempotent: it installs on whatever has not joined, so it doubles as the retry.
	const first = frm.doc.status === "Draft";
	frm.add_custom_button(first ? __("Set Up Cluster") : __("Set Up Again"), () => {
		frappe.confirm(
			first
				? __("Install Garage on this cluster's machines?")
				: __(
						"Set up every machine that has not joined yet? Machines already in the cluster are left alone."
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

	if (frm.doc.status === "Draft") return;

	add_release_button(frm);
}

// Fetched only when asked for, never with the form: it can rewrite the layout and every
// bucket, and the server records each time it is shown.
function add_admin_token_button(frm) {
	frm.add_custom_button(__("Show Admin Token"), () =>
		frm.call("reveal_admin_token").then(({ message: token }) => {
			const dialog = new frappe.ui.Dialog({
				title: __("Admin Token"),
				fields: [
					{
						fieldtype: "HTML",
						options: `<p class="text-muted">${__(
							"Full control of this cluster's Garage: layout, buckets and keys. Do not paste it anywhere it will be kept."
						)}</p>`,
					},
					{ fieldname: "token", fieldtype: "Code", read_only: 1, default: token },
				],
				primary_action_label: __("Copy"),
				primary_action: () => {
					frappe.utils.copy_to_clipboard(token);
					dialog.hide();
				},
			});

			dialog.show();
			frm.reload_doc();
		})
	);
}

// A failed run leaves its machines alone, so this is the only thing that terminates one.
// The cluster's own table is the list: which machine failed is in Error and the setup log,
// and nothing is preselected.
function add_release_button(frm) {
	frm.add_custom_button(__("Release Machines"), () => {
		const machines = frm.doc.machines || [];
		if (!machines.length) {
			frappe.msgprint({
				title: __("Nothing to release"),
				message: __("This cluster has no machines."),
				indicator: "blue",
			});
			return;
		}

		show_release_dialog(frm, machines);
	});
}

function show_release_dialog(frm, machines) {
	const dialog = new frappe.ui.Dialog({
		title: __("Release Machines"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p>${__(
					"Releasing a machine asks Atlas to terminate it — the machine and its disk are gone for good. Setting up again reinstalls whatever is left, so release only what you do not want retried."
				)}</p>`,
			},
			{
				fieldname: "machines",
				fieldtype: "MultiCheck",
				label: __("Machines"),
				options: machines.map((row) => ({
					label: `${row.machine} — ${row.role}`,
					value: row.machine,
				})),
			},
		],
		primary_action_label: __("Release"),
		primary_action: ({ machines: selected }) => {
			if (!selected?.length) {
				frappe.msgprint(__("Pick at least one machine."));
				return;
			}

			dialog.hide();
			frm.call("release_machines", { machines: selected }).then(() => {
				frappe.show_alert({
					message: __("Released {0} machine(s).", [selected.length]),
					indicator: "orange",
				});
				frm.reload_doc();
			});
		},
	});

	dialog.show();
}

// vCPU and RAM are per machine; the disk is what Garage weights a storage node by.
function ask_for_machine(frm, method, title, disk_description, disk_default) {
	frappe.prompt(
		[
			{ fieldname: "cpu", label: __("vCPUs"), fieldtype: "Int", default: 2, reqd: 1 },
			{ fieldname: "ram_gb", label: __("RAM (GB)"), fieldtype: "Int", default: 4, reqd: 1 },
			{
				fieldname: "disk_gb",
				label: __("Disk (GB)"),
				description: disk_description,
				fieldtype: "Int",
				default: disk_default,
				reqd: 1,
			},
		],
		(values) =>
			frm.call(method, values).then(() => {
				frappe.show_alert({
					message: __("Asked Atlas for the machine. It joins once it boots."),
					indicator: "blue",
				});
				frm.reload_doc();
			}),
		title,
		__("Ask Atlas")
	);
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
