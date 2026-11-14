-- title: Telemetry volume grew
-- severity: Low
-- resource_type: Region
-- runbook_url: https://github.com/frappe/cargo/blob/develop/beacon/runbooks/telemetry-volume.md
-- interval: 3600
-- pending_for: 0
-- resolve_after: 86400
-- Datum counts the samples and log lines of each sender each day. A sender that sent more than 2 times its median of the 7 days before, and more than 100,000 rows, often runs a new version that sends a new metric or logs too much. The rule finds it before the storage cost does.
SELECT {region:String} AS resource_id, sender AS alert_key, toFloat64(yesterday) AS value,
       concat(sender, ' sent ', toString(yesterday), ' rows yesterday, ', toString(round(yesterday / median, 1)), ' times its 7-day median') AS detail
FROM (
    SELECT sender, sumIf(rows, date = yesterday()) AS yesterday, medianIf(rows, date < yesterday()) AS median
    FROM (
        SELECT date, sender, sum(rows) AS rows
        FROM (
            SELECT date, resource_id AS sender, metric_count AS rows FROM datum.daily_ingestion_stats
            WHERE date >= yesterday() - 7 AND date <= yesterday()
            UNION ALL
            SELECT date, resource_id AS sender, log_count AS rows FROM datum.daily_log_stats
            WHERE date >= yesterday() - 7 AND date <= yesterday()
        )
        GROUP BY date, sender
    )
    GROUP BY sender
)
WHERE yesterday > 100000 AND median > 0 AND yesterday > 2 * median
