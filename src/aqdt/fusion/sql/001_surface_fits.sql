-- @spec FUS-STORE-005
create table if not exists surface_fits (
  hour            timestamptz not null,
  status          text not null,
  n_sensors       integer not null,
  n_points        integer not null,
  variogram_model text,
  sill            double precision,
  range_m         double precision,
  nugget          double precision,
  q1              double precision,
  q2              double precision,
  cr              double precision,
  resolution_m    double precision,
  width           integer,
  height          integer,
  surface_uri     text,
  surface_sha256  text,
  primary key (hour)
);
