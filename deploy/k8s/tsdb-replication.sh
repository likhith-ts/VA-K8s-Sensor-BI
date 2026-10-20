#!/bin/bash
# Runs once on the primary during initdb: creates the streaming-replication
# login and lets it connect. The replica (ordinal 1) clones from the primary
# with pg_basebackup in its init container and never runs initdb.
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD '${REPLICATION_PASSWORD}';"
echo "host replication replicator all scram-sha-256" >> "$PGDATA/pg_hba.conf"
