-- title: Object storage health
-- severity: High
-- resource_type: Cargo Server
-- runbook_url: https://github.com/frappe/cargo/blob/develop/beacon/runbooks/object-storage-health.md
-- interval: 60
-- pending_for: 180
-- resolve_after: 300
-- Cargo reads the health of each cluster from its gateway every minute and sends the verdict. Critical pages. Degraded, Unknown, or no verdict for 10 minutes from a cluster that sent one in the last 14 days, is Medium.
SELECT 'cargo' AS resource_id, cluster AS alert_key, toFloat64(dateDiff('second', last_ts, now())) AS value,
       if(last_ts < now() - INTERVAL 10 MINUTE, concat('Cargo sent no health of ', cluster, ' for ', toString(round(value / 60)), ' min'),
          concat('object storage cluster ', cluster, ' is ', health)) AS detail,
       if(health = 'Critical' AND last_ts >= now() - INTERVAL 10 MINUTE, 'High', 'Medium') AS severity
FROM (
    SELECT labels['cluster'] AS cluster, argMax(labels['health'], ts) AS health, max(ts) AS last_ts
    FROM datum.samples
    WHERE metric = 'cargo_object_storage_health' AND ts > now() - INTERVAL 14 DAY
      AND resource_id IN (SELECT DISTINCT resource_id FROM datum.daily_ingestion_stats WHERE date > today() - 14 AND startsWith(resource_id, 'cargo'))
    GROUP BY cluster
)
WHERE health != 'Healthy' OR last_ts < now() - INTERVAL 10 MINUTE
