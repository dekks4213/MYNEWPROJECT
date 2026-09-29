<#
.SYNOPSIS
  RITM PostgreSQL backup (Windows / PowerShell, Docker Desktop).
.DESCRIPTION
  Creates a custom-format pg_dump inside the db container, copies it to the backup folder
  and deletes backups older than -KeepDays. Binary data never goes through PowerShell
  pipes/redirection (which would corrupt it).
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\backup.ps1 -Dir D:\ritm-backups -KeepDays 14
#>
param(
  [string]$Dir = (Join-Path $PSScriptRoot "..\backups"),
  [int]$KeepDays = 14
)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$name = "ritm-$stamp.dump"
$target = Join-Path (Resolve-Path $Dir) $name

docker compose exec -T db pg_dump -U postgres -d fitcoach -Fc -f "/tmp/$name"
if ($LASTEXITCODE -ne 0) { throw "pg_dump failed" }
docker compose cp "db:/tmp/$name" "$target"
if ($LASTEXITCODE -ne 0) { throw "copy failed" }
docker compose exec -T db rm -f "/tmp/$name" | Out-Null

$size = (Get-Item $target).Length
if ($size -lt 1024) { throw "backup looks empty ($size bytes)" }
Write-Host "Backup written: $target ($size bytes)"

Get-ChildItem -Path $Dir -Filter "ritm-*.dump" |
  Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } |
  ForEach-Object { Remove-Item $_.FullName; Write-Host "Removed old backup $($_.Name)" }
