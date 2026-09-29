#!/bin/sh
# RITM PostgreSQL backup (Linux/macOS/WSL). Usage: scripts/backup.sh [DIR] [KEEP_DAYS]
set -eu
cd "$(dirname "$0")/.."
DIR="${1:-backups}"
KEEP_DAYS="${2:-14}"
mkdir -p "$DIR"
NAME="ritm-$(date +%Y%m%d-%H%M%S).dump"
docker compose exec -T db pg_dump -U postgres -d fitcoach -Fc -f "/tmp/$NAME"
docker compose cp "db:/tmp/$NAME" "$DIR/$NAME"
docker compose exec -T db rm -f "/tmp/$NAME"
SIZE=$(wc -c < "$DIR/$NAME")
[ "$SIZE" -ge 1024 ] || { echo "backup looks empty ($SIZE bytes)"; exit 1; }
echo "Backup written: $DIR/$NAME ($SIZE bytes)"
find "$DIR" -name 'ritm-*.dump' -mtime +"$KEEP_DAYS" -print -delete
