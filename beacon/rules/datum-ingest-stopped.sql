-- title: Datum stores no new samples
-- severity: High
-- resource_type: Region
-- runbook_url: https://github.com/frappe/cargo/blob/develop/beacon/runbooks/datum-ingest-stopped.md
-- interval: 60
-- pending_for: 60
-- resolve_after: 180
-- Every machine of the region sends each minute, so no new sample for 3 minutes means the Datum API, its proxy or its writes stopped.
-- An open incident on the region covers the machine-silent alerts that follow.
-- A sample more than 1 minute in the future does not count, so a sender with a fast clock cannot hide the stop.
SELECT {region:String} AS resource_id, '' AS alert_key, toFloat64(dateDiff('second', max(ts), now())) AS value,
       concat('Datum stored no new sample for ', toString(round(value / 60, 1)), ' min') AS detail
FROM datum.samples_recent
WHERE ts > now() - INTERVAL 1 DAY AND ts < now() + INTERVAL 1 MINUTE
HAVING value >= 180
