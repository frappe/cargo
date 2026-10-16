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

OVERVIEW = [
	("healthy", "Cluster Healthy", WHOLE),
	("reporting_nodes", "Nodes Reporting", WHOLE),
	("storage_nodes_ok", "Storage Nodes Up", WHOLE),
	("connected_nodes", "Connected Nodes", WHOLE),
	("max_disk_used", "Fullest Disk", PERCENT),
	("requests_per_second", "S3 Requests / s", {"decimals": 2}),
	("errors_per_second", "S3 Errors / s", {"decimals": 3}),
	("latency_ms", "S3 Avg Latency", {"decimals": 0, "suffix": " ms"}),
	("max_cpu", "Busiest CPU", PERCENT),
	("max_memory", "Highest Memory", PERCENT),
]

NODE_TIMELINE = {
	"x_column": dimension("time", "Datetime", "minute"),
	"y_column": dimension("node"),
	"value_column": {**measure("State", "level"), "data_type": "Integer"},
	"min": 0,
	"max": 3,
	"palette": "sequential",
	"show_values": False,
	"number_format": WHOLE,
}

NODES = table(
	rows=[
		dimension("cluster"),
		dimension("machine"),
		dimension("role"),
		dimension("node_id"),
		dimension("last_seen", "Datetime", "second"),
		dimension("last_restart", "Datetime", "second"),
	],
	values=[
		measure("Reporting", "reporting"),
		measure("Restarts 7d", "restarts_7d"),
		measure("Missed Scrapes 7d", "missed_scrapes_7d"),
		measure("Seen Disconnected 7d", "disconnects_7d"),
	],
	order_by="Restarts 7d",
)

CLUSTERS = table(
	rows=[dimension("cluster"), dimension("region")],
	values=[
		measure("Healthy", "healthy", "min"),
		measure("Available", "available", "min"),
		measure("Storage Nodes OK", "storage_nodes_ok", "min"),
		measure("Storage Nodes", "storage_nodes"),
		measure("Partitions Quorum", "partitions_quorum", "min"),
		measure("Partitions", "partitions"),
		measure("Connected Nodes", "connected_nodes", "min"),
		measure("Known Nodes", "known_nodes"),
		measure("Reporting Machines", "reporting_machines"),
	],
)

DISK_USAGE = table(
	rows=[dimension("cluster"), dimension("machine"), dimension("role"), dimension("volume")],
	values=[
		measure("Total GB", "total_gb"),
		measure("Free GB", "free_gb", "min"),
		measure("Used %", "used_percent"),
	],
	order_by="Used %",
)

NODE_EVENTS = table(
	rows=[
		dimension("ts", "Datetime", "second"),
		dimension("machine"),
		dimension("role"),
		dimension("event"),
		dimension("detail"),
	],
	values=[{**measure("Events", "count", "count"), "data_type": "Integer"}],
	order_by="ts",
)

MACHINES = table(
	rows=[
		dimension("cluster"),
		dimension("machine"),
		dimension("role"),
		dimension("last_seen", "Datetime", "second"),
	],
	values=[
		measure("CPU %", "cpu_percent"),
		measure("Memory %", "memory_percent"),
		measure("Load / Core", "load_per_core"),
		measure("Root Disk %", "root_disk_percent"),
		measure("In MB/s", "received_mb"),
		measure("Out MB/s", "sent_mb"),
	],
	number_formats={
		"CPU %": PERCENT,
		"Memory %": PERCENT,
		"Load / Core": {"decimals": 2},
		"Root Disk %": PERCENT,
		"In MB/s": {"decimals": 3},
		"Out MB/s": {"decimals": 3},
	},
)

LOAD_LINE_AT_1 = [{"axis": "y", "value": 1, "label": "1", "color": "#e03636", "dashed": True}]


def add_object_storage_dashboard(workbook: Workbook) -> None:
	"""Garage cluster health, the machines it runs on, the S3 API, storage and cluster internals."""
	dashboard = Dashboard(workbook, "Cargo - Object Storage", "cargo-object-storage")
	chart = dashboard.chart

	dashboard.heading("Overview")
	dashboard.numbers(
		"overview",
		"Overview",
		OVERVIEW,
		"Health is the worst across nodes over the last 15 minutes. S3 covers the last 15 minutes, CPU the last 5.",
	)

	dashboard.heading("Nodes")
	chart("node-timeline", "Node Health Timeline", "Heatmap", NODE_TIMELINE,
		"One cell per scrape. Light is OK, 1 seen disconnected by peers (cluster row: unhealthy), 2 no data, 3 restarted.",
		width=20, height=10)  # fmt: skip
	chart("nodes", "Nodes", "Table", NODES,
		"Each Garage node, whether it reports now, and its trouble over the last 7 days.", width=20, height=13)  # fmt: skip
	chart("clusters", "Clusters", "Table", CLUSTERS,
		"Latest Garage cluster state per object storage cluster.", width=20, height=8)  # fmt: skip
	chart("disk-usage", "Disk Usage", "Table", DISK_USAGE,
		"Latest disk usage per machine and Garage volume.", width=20, height=19)  # fmt: skip
	chart("node-events", "Node Events", "Table", NODE_EVENTS,
		"Restarts, missed scrapes, peer disconnects and cluster-unhealthy ticks, last 7 days.", width=20, height=8)  # fmt: skip

	dashboard.heading("Machines")
	chart("machines", "Machines", "Table", MACHINES,
		"Latest host metrics from Vector on each Garage machine. CPU and network cover the last 5 minutes.",
		width=20, height=13)  # fmt: skip
	chart("host-cpu", "CPU Busy", "Line", line("CPU", "%", PERCENT, max=100, reference_lines=RED_LINE_AT_80),
		"Share of CPU time not idle, per machine.")  # fmt: skip
	chart("host-memory", "Memory Used", "Line", line("Memory", "%", PERCENT, max=100, reference_lines=RED_LINE_AT_80),
		"Memory in use (total minus available), per machine.")  # fmt: skip
	chart("host-load", "Load per Core", "Line", line("Load", "per core", {"decimals": 2}, reference_lines=LOAD_LINE_AT_1),
		"5 minute load average divided by CPU count. Above 1 means work is queuing.", height=14)  # fmt: skip
	chart("host-network", "Network", "Line", line("MB/s", "MB / s", MB_PER_SECOND),
		"MB per second received (in) and sent (out), per machine.", height=14)  # fmt: skip
	chart("host-disk", "Disk I/O", "Line", line("MB/s", "MB / s", MB_PER_SECOND),
		"MB per second read from and written to the machine disk.", width=20, height=13)  # fmt: skip

	dashboard.heading("S3 API")
	chart("s3-requests", "S3 Requests / s", "Line", line("req/s", "requests / s", {"decimals": 2}, "sum"),
		"Requests per second by S3 endpoint, between Garage scrapes (every 5 minutes).")  # fmt: skip
	chart("s3-errors", "S3 Errors / s", "Line", line("err/s", "errors / s", {"decimals": 3}, "sum"),
		"Errors per second by status code and endpoint. Empty means no errors.")  # fmt: skip
	chart("s3-latency", "S3 Avg Latency", "Line", line("Latency", "ms", {"decimals": 0, "suffix": " ms"}, "avg"),
		"Average request time by endpoint. Gaps mean no requests to that endpoint.", width=20)  # fmt: skip

	dashboard.heading("Storage")
	chart("garage-disk-used", "Garage Disk Used", "Line", line("Used", "%", PERCENT, max=100, reference_lines=RED_LINE_AT_80),
		"Garage data volume used share per node.")  # fmt: skip
	chart("garage-block-io", "Garage Block I/O", "Line", line("MB/s", "MB / s", MB_PER_SECOND, "sum"),
		"MB per second Garage read from and wrote to its data blocks, per node.")  # fmt: skip
	chart("resync", "Resync Queue / Errored Blocks", "Line", line("Blocks", "blocks", WHOLE),
		"Blocks waiting to be copied between nodes, and blocks that failed. Errored should stay at 0.", width=20)  # fmt: skip

	dashboard.heading("Cluster Internals")
	chart("connected-nodes", "Connected Nodes", "Line", line("Connected", "nodes", WHOLE),
		"Peers each node can see. A dip on one node only shows which link broke.")  # fmt: skip
	chart("rpc-errors", "RPC Errors and Timeouts / s", "Line", line("per s", "per s", {"decimals": 2}, "sum"),
		"Failed and timed out calls between Garage nodes, per second. Should stay at 0.")  # fmt: skip
