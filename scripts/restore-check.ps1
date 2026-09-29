<#
.SYNOPSIS
  Verifies that a RITM backup can be restored, without touching the live database.
.DESCRIPTION
  Restores the dump into a temporary database "ritm_restore_check" in the same container,
  compares row counts of key tables with the live database, then drops the temporary DB.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\restore-check.ps1 -File backups\ritm-20260929-030000.dump
#>
param([Parameter(Mandatory = $true)][string]$File)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if (-not (Test-Path $File)) { throw "file not found: $File" }
$tmp = "/tmp/restore-check.dump"
$tables = "users food_entries weight_entries workout_sessions activity_types foods saved_meals reminders"

docker compose cp "$File" "db:$tmp"
docker compose exec -T db dropdb -U postgres --if-exists ritm_restore_check
docker compose exec -T db createdb -U postgres -O fitcoach_owner ritm_restore_check
docker compose exec -T db pg_restore -U postgres -d ritm_restore_check --exit-on-error $tmp
if ($LASTEXITCODE -ne 0) { throw "pg_restore failed" }
$failed = $false
foreach ($t in $tables.Split(" ")) {
  $restored = (docker compose exec -T db psql -U postgres -d ritm_restore_check -Atc "SELECT count(*) FROM $t").Trim()
  $live = (docker compose exec -T db psql -U postgres -d fitcoach -Atc "SELECT count(*) FROM $t").Trim()
  Write-Host ("{0,-20} restored={1,-8} live={2}" -f $t, $restored, $live)
}
$rls = (docker compose exec -T db psql -U postgres -d ritm_restore_check -Atc "SELECT count(*) FROM pg_policies").Trim()
Write-Host "RLS policies in restored DB: $rls"
if ([int]$rls -lt 18) { $failed = $true; Write-Host "Expected at least 18 RLS policies" }
docker compose exec -T db dropdb -U postgres ritm_restore_check
docker compose exec -T db rm -f $tmp | Out-Null
if ($failed) { throw "restore check FAILED" }
Write-Host "Restore check OK (live counts may be higher if data changed after the backup)."
