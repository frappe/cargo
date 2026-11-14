from workbook import (
	MB_PER_SECOND,
	PERCENT,
	RED_LINE_AT_80,
	WHOLE,
	Dashboard,
	Workbook,
	dimension,
	line,
	measure,
	table,
)

GB = {"decimals": 2, "suffix": " GB"}

OVERVIEW = [
	("reporting", "Reporting", WHOLE),
	("cpu_percent", "CPU", PERCENT),
	("memory_percent", "Memory", PERCENT),
	("disk_percent", "Disk Used", PERCENT),
	("disk_free_gb", "Disk Free", GB),
	("error_logs_1h", "Error Logs 1h", WHOLE),
]

ERRORS = table(
	rows=[
		dimension("last_seen", "Datetime", "second"),
		dimension("service"),
		dimension("level"),
		dimension("source"),
		dimension("message"),
	],
	values=[{**measure("Count", "count", "sum"), "data_type": "Integer"}],
	order_by="last_seen",
)


def add_cargo_dashboard(workbook: Workbook) -> None:
	"""The Cargo VM and site: reachability, disk, machine metrics, site uptime and logs."""
	dashboard = Dashboard(workbook, "Cargo", "cargo")
	chart = dashboard.chart

	dashboard.heading("Overview")
	dashboard.numbers(
		"cargo-overview",
		"Cargo Overview",
		OVERVIEW,
		"Machine readings from the last 5 minutes, error logs from the last hour. Reporting is 0 when the Cargo VM sent no machine metrics for 2 minutes.",
	)
	chart("cargo-readiness", "Atlas Readiness", "Line", line("Ready", "%", PERCENT, "avg", max=100),
		"Share of Atlas readiness checks the Cargo node passed, per 15 minutes, last 7 days.", width=20, height=10)  # fmt: skip

	dashboard.heading("Disk")
	chart("cargo-disk-used", "Disk Used", "Line", line("Used", "%", PERCENT, max=100, reference_lines=RED_LINE_AT_80),
		"Root disk used share, last 7 days. A full disk stops the bench and its telemetry.")  # fmt: skip
	chart("cargo-disk-free", "Disk Free / Total", "Line", line("GB", "GB", GB, "min"),
		"Free and total GB on the root disk, last 7 days. A step in total is a resize.")  # fmt: skip

	dashboard.heading("Machine")
	chart("cargo-cpu", "CPU", "Line", line("CPU", "%", PERCENT, max=100, reference_lines=RED_LINE_AT_80),
		"Busy share (not idle and not iowait), and time waiting on disk (iowait).")  # fmt: skip
	chart("cargo-memory", "Memory", "Line", line("GB", "GB", GB),
		"Total, used (total minus available) and swap used.")  # fmt: skip
	chart("cargo-load", "Load Average", "Line", line("Load", "load", {"decimals": 2}),
		"1, 5 and 15 minute load averages.")  # fmt: skip
	chart("cargo-network", "Network", "Line", line("MB/s", "MB / s", MB_PER_SECOND),
		"MB per second received (in) and sent (out).")  # fmt: skip
	chart("cargo-disk-io", "Disk I/O", "Line", line("MB/s", "MB / s", MB_PER_SECOND),
		"MB per second read from and written to disk.", width=20, height=10)  # fmt: skip

	dashboard.heading("Site")
	chart("cargo-uptime", "Uptime Check", "Line", line("Sites", "sites", WHOLE),
		"Sites on the Cargo VM in each state at Pilot's uptime check. Above 0 on 'down' is a failed check.", width=20, height=10)  # fmt: skip

	dashboard.heading("Logs")
	chart("cargo-error-volume", "Warnings and Errors", "Line", line("Lines", "lines / 15 min", WHOLE, "sum"),
		"Journal lines of the Cargo VM and Cargo Error Logs above info, per 15 minutes, by service and level, last 7 days.", width=20, height=10)  # fmt: skip
	chart("cargo-errors", "Recent Warnings and Errors", "Table", ERRORS,
		"Distinct warning and error messages, last 7 days. Job names and timestamps are masked so repeats group.",
		width=20, height=14)  # fmt: skip
