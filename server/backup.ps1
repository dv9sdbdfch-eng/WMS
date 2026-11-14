<#
Store WMS - backup of the shared database (run by Windows Task Scheduler, set up by setup_server.ps1).

  -Kind HOURLY   working hours, kept 2 days
  -Kind NIGHTLY  22:00, kept 30 days (also copies the newest to OffsiteDir if set)
  -Kind WEEKLY   Sunday, kept 8 weeks, always copied to OffsiteDir if set

Each backup is a pg_dump "custom" file, checked by reading its table of contents back. The result
(good or failed) is written into the database (table server_backups), so every PC's Store WMS
shows when the last good backup was made and warns when there has been none for a day.
#>
param([ValidateSet("HOURLY", "NIGHTLY", "WEEKLY")][string]$Kind = "NIGHTLY")
$ErrorActionPreference = "Stop"
$home_ = Join-Path $env:ProgramData "StoreWMS-Server"
$cfg = Get-Content (Join-Path $home_ "server.json") -Raw | ConvertFrom-Json
$env:PGPASSFILE = Join-Path $home_ "pgpass.conf"
$pgDump = Join-Path $cfg.PgBin "pg_dump.exe"
$pgRestore = Join-Path $cfg.PgBin "pg_restore.exe"
$psql = Join-Path $cfg.PgBin "psql.exe"
$keepDays = @{ HOURLY = 2; NIGHTLY = 30; WEEKLY = 56 }[$Kind]
$dir = Join-Path $cfg.BackupDir $Kind.ToLower()
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$file = Join-Path $dir ("storewms_{0}_{1:yyyy-MM-dd_HHmm}.backup" -f $Kind.ToLower(), (Get-Date))
$log = Join-Path $home_ "backup.log"

function Record([bool]$ok, [string]$name, [long]$size, [string]$msg) {
    $m = ($msg -replace "'", "''")
    if ($m.Length -gt 1000) { $m = $m.Substring(0, 1000) }
    $sql = "INSERT INTO server_backups (finished_at, kind, ok, file_name, size_bytes, message) VALUES " +
           "(now() AT TIME ZONE 'UTC', '$Kind', $($ok.ToString().ToLower()), '$name', $size, '$m')"
    & $psql -h 127.0.0.1 -p $cfg.Port -U $cfg.AppUser -d $cfg.Database -v ON_ERROR_STOP=1 -q -c $sql 2>&1 | Out-Null
}

try {
    $tmp = "$file.part"
    & $pgDump -h 127.0.0.1 -p $cfg.Port -U $cfg.AppUser -d $cfg.Database -Fc -Z 6 -f $tmp 2>&1 | Tee-Object -FilePath $log -Append | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed (exit $LASTEXITCODE), see $log" }
    # a backup that cannot be read back is not a backup
    $toc = & $pgRestore --list $tmp 2>&1
    if ($LASTEXITCODE -ne 0 -or -not ($toc -match "TABLE DATA public stock_movements")) { throw "The new backup cannot be read back." }
    Move-Item $tmp $file -Force
    $size = (Get-Item $file).Length
    if ($cfg.OffsiteDir -and ($Kind -ne "HOURLY")) {
        New-Item -ItemType Directory -Force -Path $cfg.OffsiteDir | Out-Null
        Copy-Item $file $cfg.OffsiteDir -Force
        Get-ChildItem $cfg.OffsiteDir -Filter "storewms_*.backup" | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-56) } | Remove-Item -Force
    }
    # keep the newest few even if the clock is wrong; delete only older files of this kind
    Get-ChildItem $dir -Filter "storewms_*.backup" | Sort-Object LastWriteTime -Descending | Select-Object -Skip 3 |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$keepDays) } | Remove-Item -Force
    Record $true (Split-Path $file -Leaf) $size ""
    Add-Content $log ("{0:u} {1} OK {2} ({3:N0} bytes)" -f (Get-Date), $Kind, $file, $size)
} catch {
    Remove-Item "$file.part" -ErrorAction SilentlyContinue
    Add-Content $log ("{0:u} {1} FAILED {2}" -f (Get-Date), $Kind, $_)
    Record $false "" 0 "$_"
    exit 1
}
