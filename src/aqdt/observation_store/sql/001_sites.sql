-- @spec OBS-PG-001, OBS-PG-002
create extension if not exists postgis;

create table if not exists sites (
  site_id            text primary key,
  source             text not null,
  source_native_id   text not null,
  site_type          text not null,
  name               text,
  latitude           double precision not null,
  longitude          double precision not null,
  geom               geometry(Point, 4326) not null
);

create index if not exists sites_geom_idx on sites using gist (geom);
