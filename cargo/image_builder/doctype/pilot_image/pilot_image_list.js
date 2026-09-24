// Copyright (c) 2026, Aradhya-Tripathi and contributors
// For license information, please see license.txt

frappe.listview_settings["Pilot Image"] = {
	get_indicator(doc) {
		const colors = {
			Provisioning: "orange",
			Building: "blue",
			Snapshotting: "blue",
			Completed: "green",
			Failed: "red",
			Retired: "gray",
		};
		return [__(doc.status), colors[doc.status], `status,=,${doc.status}`];
	},
};
