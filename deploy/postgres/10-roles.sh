#!/bin/sh
# Runs once, on first initialization of an empty data volume.
# Creates a table-owner role for migrations and a separate non-owner runtime role
# that cannot bypass row-level security.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v owner_pw="$FITCOACH_OWNER_PASSWORD" -v app_pw="$FITCOACH_APP_PASSWORD" <<'SQL'
CREATE ROLE fitcoach_owner LOGIN PASSWORD :'owner_pw' NOSUPERUSER NOBYPASSRLS NOCREATEROLE;
CREATE ROLE fitcoach_app LOGIN PASSWORD :'app_pw'
  NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
ALTER DATABASE fitcoach OWNER TO fitcoach_owner;
ALTER SCHEMA public OWNER TO fitcoach_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE fitcoach FROM PUBLIC;
GRANT CONNECT ON DATABASE fitcoach TO fitcoach_owner, fitcoach_app;
SQL
