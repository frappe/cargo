SELECT ts AS time, if(attributes['up'] = 'true', 'response', 'down') AS series,
  if(attributes['up'] = 'true', toFloat64OrZero(attributes['response_ms']), 0) AS value
FROM logs
WHERE resource_id LIKE 'cargo:%' AND source = 'uptime.json.log' AND ts > now() - INTERVAL 24 HOUR
