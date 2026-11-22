-- title: Telemetry events dropped
-- description: The Datum agent dropped 100 or more events in 1 hour. The machine's telemetry has a gap.
-- severity: Low
-- resource_type: Metal Host, Virtual Machine
-- runbook_url: https://github.com/frappe/cargo/blob/develop/beacon/runbooks/telemetry-dropped.md
-- interval: 300
-- pending_for: 300
-- resolve_after: 3600
-- The Datum agent counts the events that it drops: lines over the log cap, events that a full buffer drops, and events lost by error. A role file that drops a line on purpose does not count. Each drop is a gap in the data of the machine, so 100 in 1 h alerts.
SELECT resource_id, labels['reason'] AS alert_key, sum(increase) AS value,
       concat(toString(value), ' events dropped in 1 h (limit 100), reason ', alert_key, ', by ',
              arrayStringConcat(arraySort(groupUniqArray(labels['component'])), ', ')) AS detail
FROM datum.counter_increase(metric = 'datum_agent_dropped_events_total', window = 3600)
GROUP BY resource_id, labels['reason']
HAVING value >= 100
