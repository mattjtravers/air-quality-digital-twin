-- @spec OBS-PG-001, OBS-PG-002
create table if not exists observations (
  site_id            text not null references sites (site_id),
  source             text not null,
  observed_at        timestamptz not null,
  latitude           double precision not null,
  longitude          double precision not null,
  pm25_raw           double precision,
  pm25_channel_a     double precision,
  pm25_channel_b     double precision,
  humidity           double precision,
  pm25_corrected     double precision,
  qc_flags           text[] not null,
  raw                jsonb not null,
  geom               geometry(Point, 4326) not null,
  primary key (site_id, observed_at)
);

create index if not exists observations_geom_idx on observations using gist (geom);
create index if not exists observations_observed_at_idx on observations using btree (observed_at);
create index if not exists observations_source_observed_at_idx
  on observations using btree (source, observed_at);
