from pathlib import Path

SQL = Path(__file__).parent / "sql"
WORKBOOK = "wb-cargo"
NO_FILTERS = {"logical_operator": "And", "filters": []}

PERCENT = {"decimals": 1, "suffix": "%"}
MB_PER_SECOND = {"decimals": 2, "suffix": " MB/s"}
WHOLE = {"decimals": 0}
RED_LINE_AT_80 = [{"axis": "y", "value": 80, "label": "80%", "color": "#e03636", "dashed": True}]


class Workbook:
	"""Collects queries, charts and dashboards, and exports them in the Insights workbook format."""

	def __init__(self, title: str, data_source: str) -> None:
		self.title = title
		self.data_source = data_source
		self.queries: dict = {}
		self.charts: dict = {}
		self.dashboards: dict = {}

	def add_chart(self, key: str, title: str, chart_type: str, config: dict, info: str = "") -> str:
		"""Add query `q-<key>` from `sql/<key>.sql` and chart `c-<key>` on it. Returns the chart name."""
		query = f"q-{key}"
		self.queries[query] = {
			"name": query,
			"title": title,
			"workbook": WORKBOOK,
			"sort_order": len(self.queries),
			"use_live_connection": 1,
			"is_native_query": 1,
			"operations": [
				{"type": "sql", "raw_sql": (SQL / f"{key}.sql").read_text(), "data_source": self.data_source}
			],
		}
		chart = f"c-{key}"
		self.charts[chart] = {
			"name": chart,
			"title": title,
			"info": info or None,
			"workbook": WORKBOOK,
			"sort_order": len(self.charts),
			"query": query,
			"chart_type": chart_type,
			"config": {"filters": NO_FILTERS, **config},
		}
		return chart

	def export(self) -> dict:
		return {
			"doctype": "Insights Workbook",
			"name": WORKBOOK,
			"title": self.title,
			"folders": [],
			"queries": self.queries,
			"charts": self.charts,
			"dashboards": self.dashboards,
		}


class Dashboard:
	"""Adds charts to the workbook and places them left to right on the 20 column grid, wrapping when full."""

	def __init__(self, workbook: Workbook, title: str, route: str) -> None:
		self.workbook = workbook
		self.items: list = []
		self.x = self.y = self.row_height = 0
		workbook.dashboards[f"dashboard-{route}"] = {
			"name": f"dashboard-{route}",
			"title": title,
			"route": route,
			"workbook": WORKBOOK,
			"vertical_compact_layout": 1,
			"items": self.items,
		}

	def place(self, item: dict, width: int, height: int) -> None:
		if self.x + width > 20:
			self.new_row()
		item["layout"] = {"i": f"item-{len(self.items)}", "x": self.x, "y": self.y, "w": width, "h": height}
		self.items.append(item)
		self.x += width
		self.row_height = max(self.row_height, height)

	def new_row(self) -> None:
		if self.x:
			self.y += self.row_height
		self.x = self.row_height = 0

	def heading(self, text: str) -> None:
		self.new_row()
		self.place({"type": "text", "text": f"<h3>{text}</h3>"}, 20, 2)
		self.new_row()

	def chart(
		self,
		key: str,
		title: str,
		chart_type: str,
		config: dict,
		info: str,
		width: int = 10,
		height: int = 12,
	) -> None:
		chart = self.workbook.add_chart(key, title, chart_type, config, info)
		self.place({"type": "chart", "chart": chart}, width, height)

	def numbers(self, key: str, title: str, readings: list[tuple[str, str, dict]], info: str) -> None:
		"""One Number chart, shown as a 4 by 4 card per (column, name, format) reading."""
		chart = self.workbook.add_chart(key, title, "Number", numbers(readings), info)
		for _, name, _ in readings:
			self.place({"type": "chart", "chart": chart, "reading": name}, 4, 4)
		self.new_row()


def measure(name: str, column: str, aggregation: str = "max") -> dict:
	return {"measure_name": name, "column_name": column, "data_type": "Decimal", "aggregation": aggregation}


def dimension(column: str, data_type: str = "String", granularity: str | None = None) -> dict:
	value = {"dimension_name": column, "column_name": column, "data_type": data_type}
	if granularity:
		value["granularity"] = granularity
	return value


def line(name: str, axis_label: str, number_format: dict, aggregation: str = "max", **y_axis) -> dict:
	"""One line per `series` over `time`. Queries return `time`, `series` and `value`."""
	return {
		"x_axis": {"dimension": dimension("time", "Datetime", "minute")},
		"y_axis": {
			"series": [{"measure": measure(name, "value", aggregation)}],
			"min": 0,
			"axis_label": axis_label,
			"show_axis_label": True,
			"show_data_points": False,
			"show_data_labels": False,
			"show_area": False,
			"smooth": False,
			"stack": False,
			**y_axis,
		},
		"split_by": {"dimension": dimension("series"), "max_split_values": 20},
		"number_formats": {name: number_format},
		"limit": 20000,
	}


def table(
	rows: list[dict], values: list[dict], order_by: str | None = None, number_formats: dict | None = None
) -> dict:
	config = {"rows": rows, "columns": [], "values": values, "limit": 200, "show_row_totals": False}
	if order_by:
		config["order_by"] = [{"column": {"type": "column", "column_name": order_by}, "direction": "desc"}]
	if number_formats:
		config["number_formats"] = number_formats
	return config


def numbers(readings: list[tuple[str, str, dict]]) -> dict:
	"""A Number chart with one reading per (column, name, format)."""
	return {
		"number_columns": [{**measure(name, column), "id": name} for column, name, _ in readings],
		"number_column_options": [{} for _ in readings],
		"number_formats": {name: number_format for _, name, number_format in readings},
	}
