# Backup the appmail_app-data Docker volume (config, tokens, credentials, orders DB, logs)
# to a folder on Windows, keeping the newest -Keep archives.
#
# Usage (PowerShell):
#   .\scripts\backup-appmail-volume.ps1                              # -> $HOME\AppMailBackup
#   .\scripts\backup-appmail-volume.ps1 -BackupDir "D:\OneDrive\AppMailBackup" -Keep 30
#
# Daily via Task Scheduler (run once in an admin-free PowerShell):
#   $a = New-ScheduledTaskAction -Execute "powershell.exe" -Argument '-NoProfile -ExecutionPolicy Bypass -File "<abs-path>\scripts\backup-appmail-volume.ps1" -BackupDir "<abs-backup-dir>"'
#   Register-ScheduledTask -TaskName "AppMail backup" -Action $a -Trigger (New-ScheduledTaskTrigger -Daily -At 2am)
#
# The live SQLite file is NOT copied directly (a copy taken mid-write can miss
# WAL pages). The app container first writes a consistent snapshot
# etsy/etsy-snapshot.db; the archive contains that instead of etsy.db.
#
# Restore on a machine (container stopped):
#   docker volume create appmail_app-data
#   docker run --rm -v appmail_app-data:/d -v "<backup-dir>:/b" alpine sh -c "tar xzf /b/<file>.tgz -C /d && mv /d/etsy/etsy-snapshot.db /d/etsy/etsy.db"
param(
    [string]$BackupDir = (Join-Path $HOME "AppMailBackup"),
    [int]$Keep = 14
)
$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force $BackupDir | Out-Null
$stamp = Get-Date -Format "yyyy-MM-dd_HHmm"
$name = "appmail-volume-$stamp.tgz"

# 1. Consistent DB snapshot inside the volume (skipped if the container is not running / no DB yet)
$running = docker ps --filter "name=^appmail$" --format "{{.Names}}"
if ($running -eq "appmail") {
    docker exec appmail python etsy_sqlite_order_store.py snapshot /data/etsy/etsy-snapshot.db
    if ($LASTEXITCODE -ne 0) { Write-Warning "Snapshot DB that bai - archive se khong co etsy-snapshot.db" }
} else {
    Write-Warning "Container appmail khong chay - chi backup file hien co"
}

# 2. Archive the volume, leaving out the live DB files
docker run --rm -v appmail_app-data:/d:ro -v "${BackupDir}:/b" alpine `
    tar czf "/b/$name" -C /d --exclude=./etsy/etsy.db --exclude=./etsy/etsy.db-wal --exclude=./etsy/etsy.db-shm .
if ($LASTEXITCODE -ne 0) { throw "tar that bai" }
Write-Output "Backup -> $(Join-Path $BackupDir $name)"

# 3. Keep only the newest $Keep archives
Get-ChildItem $BackupDir -Filter "appmail-volume-*.tgz" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip $Keep |
    Remove-Item -Force
