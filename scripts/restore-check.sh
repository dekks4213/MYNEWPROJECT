#!/bin/sh
# Restore a dump into a temporary database and compare counts. Usage: scripts/restore-check.sh FILE
set -eu
cd "$(dirname "$0")/.."
FILE="${1:?usage: restore-check.sh FILE}"
TMP=/tmp/restore-check.dump
docker compose cp "$FILE" "db:$TMP"
docker compose exec -T db dropdb -U postgres --if-exists ritm_restore_check
docker compose exec -T db createdb -U postgres -O fitcoach_owner ritm_restore_check
docker compose exec -T db pg_restore -U postgres -d ritm_restore_check --exit-on-error "$TMP"
for t in users food_entries weight_entries workout_sessions activity_types foods saved_meals reminders; do
  R=$(docker compose exec -T db psql -U postgres -d ritm_restore_check -Atc "SELECT count(*) FROM $t")
  L=$(docker compose exec -T db psql -U postgres -d fitcoach -Atc "SELECT count(*) FROM $t")
  printf '%-20s restored=%-8s live=%s\n' "$t" "$R" "$L"
done
RLS=$(docker compose exec -T db psql -U postgres -d ritm_restore_check -Atc "SELECT count(*) FROM pg_policies")
echo "RLS policies in restored DB: $RLS"
docker compose exec -T db dropdb -U postgres ritm_restore_check
docker compose exec -T db rm -f "$TMP"
[ "$RLS" -ge 18 ] || { echo "restore check FAILED: expected >= 18 RLS policies"; exit 1; }
echo "Restore check OK"
