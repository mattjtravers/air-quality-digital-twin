-- @spec CAL-STORE-006
create table if not exists calibrated_hourly (
  site_id              text not null references sites (site_id),
  hour                 timestamptz not null,
  pm25_calibrated      double precision,
  pm25_corrected_mean  double precision not null,
  n_snapshots          integer not null,
  fit_as_of            timestamptz,
  fit_status           text not null,
  primary key (site_id, hour)
);

create index if not exists calibrated_hourly_hour_idx on calibrated_hourly using btree (hour);
