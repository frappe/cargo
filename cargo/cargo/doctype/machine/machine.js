// Copyright (c) 2026, Aradhya-Tripathi and contributors
// For license information, please see license.txt

frappe.ui.form.on("Machine", {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.vm_id) return;

		add_power_buttons(frm);
		add_resize_buttons(frm);
	},
});

const POWER_ACTIONS = [
	{ action: "start", label: __("Start") },
	{
		action: "stop",
		label: __("Stop"),
		confirm: __("Stop this machine? Its services go down until it starts again."),
	},
	{
		action: "restart",
		label: __("Restart"),
		confirm: __("Restart this machine? Its services go down while it boots."),
	},
	{
		action: "pause",
		label: __("Pause"),
		confirm: __("Pause this machine? It stops answering until resumed."),
	},
	{ action: "resume", label: __("Resume") },
];

const POWER_ACTIONS_BY_STATUS = {
	Running: ["stop", "restart", "pause"],
	Stopped: ["start"],
	Paused: ["resume", "stop"],
	Broken: ["start", "stop", "restart"],
};

function add_power_buttons(frm) {
	const allowed = POWER_ACTIONS_BY_STATUS[frm.doc.status] || [];
	for (const { action, label, confirm } of POWER_ACTIONS) {
		if (!allowed.includes(action)) continue;

		frm.add_custom_button(
			label,
			() =>
				confirm
					? frappe.confirm(confirm, () => change_power_state(frm, action, label))
					: change_power_state(frm, action, label),
			__("Power")
		);
	}
}

function change_power_state(frm, action, label) {
	frm.call("change_power_state", { action }).then(() =>
		frappe.show_alert({
			message: __("Asked Atlas to {0} the machine.", [label.toLowerCase()]),
			indicator: "blue",
		})
	);
}

function add_resize_buttons(frm) {
	if (frm.doc.status === "Stopped") {
		frm.add_custom_button(__("Resize"), () => ask_for_shape(frm), __("Resize"));
	}
	if (["Running", "Stopped"].includes(frm.doc.status)) {
		frm.add_custom_button(__("Grow Disk"), () => ask_for_disk(frm), __("Resize"));
	}
}

function ask_for_shape(frm) {
	frappe.prompt(
		[
			{
				fieldtype: "HTML",
				options: `<p class="text-muted">${__(
					"Atlas resizes it in place or moves it to a host with room. The disk only grows."
				)}</p>`,
			},
			{
				fieldname: "cpu_millicores",
				label: __("CPU (millicores)"),
				description: __("1000 millicores equals one CPU core."),
				fieldtype: "Int",
				default: frm.doc.cpu_millicores,
				reqd: 1,
			},
			{
				fieldname: "ram_gb",
				label: __("RAM (GB)"),
				fieldtype: "Int",
				default: frm.doc.ram_gb,
				reqd: 1,
			},
			{
				fieldname: "disk_gb",
				label: __("Disk (GB)"),
				fieldtype: "Int",
				default: frm.doc.disk_size_gb,
				reqd: 1,
			},
		],
		(values) =>
			frm.call("resize_machine", values).then(() => {
				frappe.show_alert({
					message: __(
						"Asked Atlas to resize the machine. The new size shows here once Atlas applies it; start it after."
					),
					indicator: "blue",
				});
				frm.reload_doc();
			}),
		__("Resize Machine"),
		__("Resize")
	);
}

function ask_for_disk(frm) {
	frappe.prompt(
		{
			fieldname: "disk_gb",
			label: __("Disk (GB)"),
			description: __("Grows while the machine runs. A disk cannot shrink."),
			fieldtype: "Int",
			default: frm.doc.disk_size_gb,
			reqd: 1,
		},
		(values) =>
			frm.call("grow_disk", values).then(() => {
				frappe.show_alert({
					message: __(
						"Asked Atlas to grow the disk. The new size shows here once Atlas applies it."
					),
					indicator: "blue",
				});
				frm.reload_doc();
			}),
		__("Grow Disk"),
		__("Grow")
	);
}
