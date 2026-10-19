-- The hourly table of features.py, as the Athena query the tuned design in
-- section 5 of the chapter runs instead of parsing raw lines in the task.
-- Reads one partition per day of the chapter 2 table; UNLOAD writes Parquet
-- that the task then loads with one call instead of 130 million json.loads.
UNLOAD (
  SELECT device,
         floor(ts / 3600) AS hour,
         avg(temp)        AS mean_temp,
         min(temp)        AS min_temp,
         max(temp)        AS max_temp,
         count(*)         AS readings
  FROM farm_telemetry.raw
  WHERE dt BETWEEN date_format(date_add('day', -90, current_date), '%Y-%m-%d')
              AND date_format(date_add('day', -1,  current_date), '%Y-%m-%d')
  GROUP BY device, floor(ts / 3600)
)
TO 's3://<artifacts-bucket>/features/dt=<run-id>/'
WITH (format = 'PARQUET', compression = 'SNAPPY');
