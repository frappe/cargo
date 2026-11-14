-- title: Telemetry dropped on machine
-- severity: Low
-- resource_type: Machine
-- runbook_url: https://github.com/frappe/cargo/blob/develop/beacon/runbooks/telemetry-dropped.md
-- interval: 300
-- pending_for: 0
-- resolve_after: 3600
-- The Datum agent counts the events that it drops: lines over the log cap, events that a full buffer drops, and events lost by error. A role file that drops a line on purpose does not count. Each drop is a gap in the data of the machine, so 100 in 1 h alerts.
SELECT resource_id, labels['reason'] AS alert_key, sum(increase) AS value,
       concat(toString(value), ' events dropped in 1 h, reason ', alert_key, ', by ',
              arrayStringConcat(arraySort(groupUniqArray(labels['component'])), ', ')) AS detail
FROM datum.counter_increase(metric = 'datum_agent_dropped_events_total', window = 3600)
GROUP BY resource_id, labels['reason']
HAVING value >= 100
