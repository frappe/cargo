SELECT ts AS time, labels['machine'] AS series, 100 * (1 - maxIf(value, metric = 'garage_local_disk_avail') / maxIf(value, metric = 'garage_local_disk_total')) AS value
FROM samples
WHERE metric IN ('garage_local_disk_avail', 'garage_local_disk_total') AND labels['volume'] = 'data' AND ts > now() - INTERVAL 24 HOUR
GROUP BY time, series
