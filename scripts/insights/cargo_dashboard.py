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
MB = {"decimals": 0, "suffix": " MB"}
MILLISECONDS = {"decimals": 0, "suffix": " ms"}

OVERVIEW = [
	("reporting", "Reporting", WHOLE),
	("processes_up", "Processes Up", WHOLE),
	("cpu_percent", "CPU", PERCENT),
	("memory_percent", "Memory", PERCENT),
	("disk_percent", "Disk Used", PERCENT),
	("disk_free_gb", "Disk Free", GB),
	("db_connections_percent", "DB Connections", PERCENT),
	("requests_1h", "Requests 1h", WHOLE),
	("server_errors_1h", "5xx 1h", WHOLE),
	("error_logs_1h", "Error Logs 1h", WHOLE),
]

PROCESSES = table(
	rows=[dimension("service"), dimension("state"), dimension("last_seen", "Datetime", "second")],
	values=[
		measure("Up", "up", "min"),
		measure("CPU %", "cpu_percent"),
		measure("Memory MB", "memory_mb"),
		measure("Open FDs", "open_fds"),
		measure("Restarts 24h", "restarts_24h"),
		measure("Down Minutes 24h", "down_minutes_24h"),
	],
	number_formats={"CPU %": PERCENT, "Memory MB": MB, "Down Minutes 24h": {"decimals": 1}},
)

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
	"""The Cargo host itself: reachability, disk, host metrics, bench processes, MariaDB, HTTP and logs."""
	dashboard = Dashboard(workbook, "Cargo", "cargo")
	chart = dashboard.chart

	dashboard.heading("Overview")
	dashboard.numbers(
		"cargo-overview",
		"Cargo Overview",
		OVERVIEW,
		"Host and process readings from the last 5 minutes, requests and errors from the last hour. Reporting is 0 when pilot sent nothing for 2 minutes.",
	)
	chart("cargo-readiness", "Atlas Readiness", "Line", line("Ready", "%", PERCENT, "avg", max=100),
		"Share of Atlas readiness checks the Cargo node passed, per 15 minutes, last 7 days.", width=20, height=10)  # fmt: skip

	dashboard.heading("Disk")
	chart("cargo-disk-used", "Disk Used", "Line", line("Used", "%", PERCENT, max=100, reference_lines=RED_LINE_AT_80),
		"Root disk used share, last 7 days. A full disk stops the bench and its telemetry.")  # fmt: skip
	chart("cargo-disk-free", "Disk Free / Total", "Line", line("GB", "GB", GB, "min"),
		"Free and total GB on the root disk, last 7 days. A step in total is a resize.")  # fmt: skip

	dashboard.heading("Host")
	chart("cargo-cpu", "CPU", "Line", line("CPU", "%", PERCENT, max=100, reference_lines=RED_LINE_AT_80),
		"Busy share, and time in user, system, iowait and steal.")  # fmt: skip
	chart("cargo-memory", "Memory", "Line", line("GB", "GB", GB),
		"Total, used, cached and swap used.")  # fmt: skip
	chart("cargo-load", "Load Average", "Line", line("Load", "load", {"decimals": 2}),
		"1, 5 and 15 minute load averages.")  # fmt: skip
	chart("cargo-network", "Network", "Line", line("MB/s", "MB / s", MB_PER_SECOND),
		"MB per second received (in) and sent (out).")  # fmt: skip
	chart("cargo-disk-io", "Disk I/O", "Line", line("MB/s", "MB / s", MB_PER_SECOND),
		"MB per second read from and written to disk.", width=20, height=10)  # fmt: skip

	dashboard.heading("Processes")
	chart("cargo-processes", "Processes", "Table", PROCESSES,
		"Each bench process pilot runs, its latest readings, and how often it went down over the last 24 hours.",
		width=20, height=9)  # fmt: skip
	chart("cargo-process-cpu", "Process CPU", "Line", line("CPU", "%", PERCENT),
		"CPU share per bench process.")  # fmt: skip
	chart("cargo-process-memory", "Process Memory", "Line", line("RSS", "MB", MB),
		"Resident memory per bench process. A steady climb is a leak.")  # fmt: skip

	dashboard.heading("Database")
	chart("cargo-db-connections", "DB Connections", "Line", line("Connections", "connections", WHOLE),
		"Open MariaDB connections against the limit. Hitting it gives 'Too many connections'.")  # fmt: skip
	chart("cargo-db-queries", "DB Queries / s", "Line", line("q/s", "queries / s", {"decimals": 1}),
		"Selects, inserts, updates and deletes per second.")  # fmt: skip
	chart("cargo-db-locks", "Row Lock Waits / Buffer Pool Misses", "Line", line("Value", "", {"decimals": 2}, "sum"),
		"Row lock waits per 5 minutes, and the share of buffer pool reads that went to disk.", width=20, height=10)  # fmt: skip

	dashboard.heading("HTTP")
	chart("cargo-http-requests", "Requests", "Line", line("Requests", "requests / 5 min", WHOLE, "sum"),
		"Requests nginx served per 5 minutes, by status class.")  # fmt: skip
	chart("cargo-http-latency", "Response Time", "Line", line("Latency", "ms", MILLISECONDS),
		"Average and 95th percentile nginx response time per 5 minutes.")  # fmt: skip
	chart("cargo-uptime", "Uptime Check", "Line", line("Response", "ms", MILLISECONDS),
		"Pilot's uptime check of the site. Points on 'down' are failed checks.", width=20, height=10)  # fmt: skip

	dashboard.heading("Logs")
	chart("cargo-error-volume", "Warnings and Errors", "Line", line("Lines", "lines / 15 min", WHOLE, "sum"),
		"Log lines above info per 15 minutes, by service and level, last 7 days.", width=20, height=10)  # fmt: skip
	chart("cargo-errors", "Recent Warnings and Errors", "Table", ERRORS,
		"Distinct warning and error messages, last 7 days. Job names and timestamps are masked so repeats group.",
		width=20, height=14)  # fmt: skip
