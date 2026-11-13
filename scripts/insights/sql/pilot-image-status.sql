SELECT ts AS time, labels['status'] AS series, count() AS value
FROM samples
WHERE resource_id LIKE 'cargo:%' AND metric = 'cargo_pilot_image_latest' AND ts > now() - INTERVAL 7 DAY
GROUP BY time, series
