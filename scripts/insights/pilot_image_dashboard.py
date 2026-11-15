from workbook import WHOLE, Dashboard, Workbook, dimension, line, measure, table

HOURS = {"decimals": 1, "suffix": " h"}
MINUTES = {"decimals": 0, "suffix": " min"}

OVERVIEW = [
	("hours_since_report", "Last Report", HOURS),
	("completed", "Latest Completed", WHOLE),
	("failed", "Latest Failed", WHOLE),
	("in_progress", "Building", WHOLE),
	("stuck", "Stuck Over 1h", WHOLE),
	("failures_7d", "Failures 7d", WHOLE),
]

LATEST = table(
	rows=[
		dimension("image_type"),
		dimension("frappe_branch"),
		dimension("signup_app"),
		dimension("image"),
		dimension("status"),
		dimension("pilot_version"),
	],
	values=[measure("Usable Image Age", "usable_age_hours")],
	number_formats={"Usable Image Age": HOURS},
)

IN_PROGRESS = table(
	rows=[
		dimension("image"),
		dimension("image_type"),
		dimension("frappe_branch"),
		dimension("signup_app"),
		dimension("status"),
	],
	values=[measure("Idle", "idle_minutes")],
	order_by="Idle",
	number_formats={"Idle": MINUTES},
)

FAILURES = table(
	rows=[
		dimension("image"),
		dimension("image_type"),
		dimension("frappe_branch"),
		dimension("signup_app"),
		dimension("pilot_version"),
		dimension("stage"),
		dimension("reason"),
		dimension("manually_failed"),
	],
	values=[measure("Retries", "retries")],
	number_formats={"Retries": WHOLE},
)


def add_pilot_image_dashboard(workbook: Workbook) -> None:
	"""Pilot image builds: whether images keep coming, which builds are stuck, and why builds fail."""
	dashboard = Dashboard(workbook, "Cargo - Pilot Images", "cargo-pilot-images")
	chart = dashboard.chart

	dashboard.heading("Overview")
	dashboard.numbers(
		"pilot-image-overview",
		"Pilot Images Overview",
		OVERVIEW,
		"From Cargo's latest hourly report. Last Report above 1 h means Cargo stopped reporting and the rest is stale.",
	)

	dashboard.heading("Latest Builds")
	chart("pilot-image-latest", "Latest Build per Image", "Table", LATEST,
		"The newest build of each image type, branch and signup app, and how old its newest usable image is. No age means it never completed.",
		width=20, height=10)  # fmt: skip
	chart("pilot-image-usable-age", "Usable Image Age", "Line", line("Age", "hours", HOURS),
		"How old the newest completed image is, last 7 days. A line that keeps climbing means building has stopped.")  # fmt: skip
	chart("pilot-image-status", "Latest Builds by Status", "Line", line("Builds", "builds", WHOLE),
		"How many of the latest builds are in each status, last 7 days.")  # fmt: skip

	dashboard.heading("Builds in Progress")
	chart("pilot-image-in-progress", "Builds in Progress", "Table", IN_PROGRESS,
		"Unfinished builds and how long since they last changed state. Over an hour usually means stuck.",
		width=20, height=6)  # fmt: skip

	dashboard.heading("Failures")
	chart("pilot-image-failures", "Failed Builds, Last 7 Days", "Table", FAILURES,
		"Where each failed build stopped and why, with how many automatic retries it used.",
		width=20, height=10)  # fmt: skip
