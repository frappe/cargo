-- title: Datum refuses writes
-- severity: Medium
-- resource_type: Region
-- runbook_url: https://github.com/frappe/cargo/blob/develop/beacon/runbooks/datum-writes-refused.md
-- interval: 300
-- pending_for: 300
-- resolve_after: 900
-- Datum logs each refused request with the resource ID of its token as `sender`, empty when the token is not valid. A refused request is
-- a gap in the data of that sender, because no sender retries. A 5xx is a fault of Datum and is High.
SELECT {region:String} AS resource_id, sender AS alert_key, toFloat64(count()) AS value,
       concat(toString(value), ' writes refused from ', if(sender = '', 'a sender with no valid token', sender), ' in 15 min, status ',
              arrayStringConcat(arraySort(groupUniqArray(status)), ', ')) AS detail,
       if(countIf(startsWith(status, '5')) > 0, 'High', 'Medium') AS severity
FROM (
    SELECT attributes['sender'] AS sender, attributes['status'] AS status
    FROM datum.logs
    WHERE ts > now() - INTERVAL 15 MINUTE AND product = 'cargo' AND service = 'datum' AND message = 'request refused'
)
GROUP BY sender
HAVING value >= 10
