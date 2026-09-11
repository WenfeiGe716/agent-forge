#!/bin/sh
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  --set forge_pass="$FORGE_DB_PASSWORD" --set temporal_pass="$TEMPORAL_DB_PASSWORD" <<'SQL'
CREATE USER forge WITH PASSWORD :'forge_pass';
CREATE DATABASE forge OWNER forge;
CREATE USER temporal WITH CREATEDB PASSWORD :'temporal_pass';
SQL
