# Cargo Insights workbook

Builds the Cargo workbook for [Insights](https://github.com/frappe/insights) from the Datum data in ClickHouse. The scripts follow [Atlas](https://github.com/frappe/atlas/tree/develop/scripts/insights), smaller.

| File | Owns |
| --- | --- |
| `sql/<key>.sql` | The query behind one chart. It returns `time`, `series` and `value` for a line chart, or the columns a table or number chart reads. |
| `object_storage_dashboard.py` | The Cargo - Object Storage dashboard: which charts, in which order, how they look. |
| `workbook.py` | `Workbook`, `Dashboard` and the chart config helpers. |
| `generate_workbook.py` | Prints the workbook as JSON. |
| `import_workbook.py` | Runs in the Insights bench and updates the workbook in place. |

## Update the workbook

Run these on the server of the Insights site, with the bench of that site. `--data-source` is the name of the Insights data source for the Datum ClickHouse, `datum` when left out.

```sh
python3 generate_workbook.py --data-source datum > /tmp/cargo-workbook.json
cd <bench>/sites && ../env/bin/python <this folder>/import_workbook.py <insights site> /tmp/cargo-workbook.json
```

The import updates the workbook titled Cargo, or creates it. Every query, chart and dashboard keeps its name, such as `c-host-cpu` or `dashboard-cargo-object-storage`, so links stay the same. It overwrites edits made in Insights, so change the scripts instead.

## Add a chart

1. Write the query in `sql/<key>.sql`. Try it in Insights first.
2. Add it to the dashboard with `chart("<key>", "<title>", "<chart type>", <config>, "<what it shows>")`. `line()` and `table()` in `workbook.py` build the config.

The key names the query `q-<key>` and the chart `c-<key>`. Keep a key once it is imported, so links to the chart keep working.
