// Copyright (c) 2026, Aradhya-Tripathi and contributors
// For license information, please see license.txt

frappe.listview_settings["Pilot Image Snapshot"] = {
	get_indicator(doc) {
		const colors = {
			Pending: "gray",
			Snapshotting: "blue",
			Available: "green",
			Unavailable: "orange",
			Failed: "red",
		};
		return [__(doc.status), colors[doc.status], `status,=,${doc.status}`];
	},
};
