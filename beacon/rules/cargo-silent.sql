-- title: Cargo silent
-- severity: Medium
-- resource_type: Cargo Server
-- interval: 900
-- pending_for: 900
-- resolve_after: 900
-- Cargo sends its image build metrics every hour. No sample for 3 hours means its scheduler, its Datum token or Cargo itself stopped.
-- No sample in 14 days also counts as silent. A region without an active Cargo has no cargo resource, so Beacon drops the row.
SELECT 'cargo' AS resource_id, '' AS alert_key, toFloat64(dateDiff('second', max(ts), now())) AS value,
       if(count() = 0, 'no Cargo metrics in 14 days', concat('no Cargo metrics for ', toString(round(value / 3600, 1)), ' h')) AS detail
FROM datum.samples
WHERE metric LIKE 'cargo_pilot_image_%' AND ts > now() - INTERVAL 14 DAY
  AND resource_id IN (SELECT DISTINCT resource_id FROM datum.daily_ingestion_stats WHERE date > today() - 14 AND startsWith(resource_id, 'cargo'))
HAVING value > 3 * 3600
