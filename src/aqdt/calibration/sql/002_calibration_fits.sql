-- @spec CAL-STORE-006
create table if not exists calibration_fits (
  site_id        text not null references sites (site_id),
  as_of          timestamptz not null,
  status         text not null,
  ref_site_id    text,
  distance_m     double precision,
  window_start   timestamptz not null,
  window_end     timestamptz not null,
  n_pairs        integer not null,
  slope          double precision,
  intercept      double precision,
  r2             double precision,
  rmse           double precision,
  primary key (site_id, as_of)
);

create index if not exists calibration_fits_as_of_idx on calibration_fits using btree (as_of);
