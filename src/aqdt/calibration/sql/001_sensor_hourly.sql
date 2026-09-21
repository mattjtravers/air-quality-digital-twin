-- @spec CAL-STORE-006
create table if not exists sensor_hourly (
  site_id              text not null references sites (site_id),
  hour                 timestamptz not null,
  pm25_corrected_mean  double precision not null,
  humidity_mean        double precision,
  n_snapshots          integer not null,
  n_flagged            integer not null,
  primary key (site_id, hour)
);

create index if not exists sensor_hourly_hour_idx on sensor_hourly using btree (hour);
