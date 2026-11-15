import argparse
import json

from cargo_dashboard import add_cargo_dashboard
from object_storage_dashboard import add_object_storage_dashboard
from pilot_image_dashboard import add_pilot_image_dashboard
from workbook import Workbook

if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="Print the Cargo Insights workbook as JSON.")
	parser.add_argument("--data-source", default="datum", help="Insights data source of the Datum ClickHouse")
	arguments = parser.parse_args()

	workbook = Workbook("Cargo", arguments.data_source)
	add_cargo_dashboard(workbook)
	add_object_storage_dashboard(workbook)
	add_pilot_image_dashboard(workbook)
	print(json.dumps(workbook.export(), indent=1))
