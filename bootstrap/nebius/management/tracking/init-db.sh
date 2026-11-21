#!/bin/sh
set -eu

MLFLOW_DB_PASSWORD=$(cat /run/tracking/db/password)
test -n "$MLFLOW_DB_PASSWORD"
export MLFLOW_DB_PASSWORD
psql --no-password --username postgres --dbname postgres --set ON_ERROR_STOP=1 <<'SQL'
\getenv app_password MLFLOW_DB_PASSWORD
CREATE ROLE mlflow LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'app_password';
CREATE DATABASE mlflow OWNER mlflow;
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;
SQL
