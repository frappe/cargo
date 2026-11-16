-- title: Datum refuses writes
-- severity: High
-- resource_type: Region
-- runbook_url: https://github.com/frappe/cargo/blob/develop/beacon/runbooks/datum-writes-refused.md
-- interval: 300
-- pending_for: 300
-- resolve_after: 900
-- Datum logs refused requests with the resource ID of the token as `sender`, at most once a minute for each sender, route and status.
-- `count` is the refusals since the line before; a line without it is one refusal. A refused request is a gap in the data of that sender,
-- because no sender retries. A 5xx is a fault of Datum and is High. Only senders with a valid token count: anyone can send an invalid
-- one, and a sender of ours that loses its token shows in its own silence rule.
SELECT {region:String} AS resource_id, sender AS alert_key, toFloat64(sum(refusals)) AS value,
       concat(toString(value), ' writes refused from ', sender, ' in 15 min, status ',
              arrayStringConcat(arraySort(groupUniqArray(status)), ', ')) AS detail,
       if(countIf(startsWith(status, '5')) > 0, 'High', 'Medium') AS severity
FROM (
    SELECT attributes['sender'] AS sender, attributes['status'] AS status,
           toUInt64OrDefault(attributes['count'], toUInt64(1)) AS refusals
    FROM datum.logs
    WHERE ts > now() - INTERVAL 15 MINUTE AND product = 'cargo' AND service = 'datum' AND message = 'request refused'
      AND attributes['sender'] != ''
      AND resource_id IN (
          SELECT labels['machine'] FROM datum.samples_recent
          WHERE metric = 'cargo_machine' AND resource_id = 'cargo' AND labels['role'] = 'telemetry'
            AND labels['machine'] NOT IN {excluded:Array(String)} AND ts > now() - INTERVAL 15 MINUTE
      )
)
GROUP BY sender
HAVING value >= 10
