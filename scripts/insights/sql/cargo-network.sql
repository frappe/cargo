SELECT ts AS time, if(metric = 'system_network_receive_bytes_per_second', 'in', 'out') AS series, value / 1048576 AS value
FROM samples
WHERE resource_id LIKE 'cargo:%' AND metric IN ('system_network_receive_bytes_per_second', 'system_network_transmit_bytes_per_second')
  AND ts > now() - INTERVAL 24 HOUR
