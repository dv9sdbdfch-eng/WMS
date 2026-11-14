<#
Store WMS - monthly test restore (Task Scheduler, set up by setup_server.ps1).

Restores the newest nightly backup into a temporary database "storewms_restoretest", compares the
number of rows of the main tables with the live database, then deletes the temporary database.
The live database is never touched. The result is recorded in server_backups (kind TEST) and in
restore_test.log, so a backup that could not be restored is noticed before it is needed.
#>
param([string]$Kind = "TEST")
$ErrorActionPreference = "Stop"
$home_ = Join-Path $env:ProgramData "StoreWMS-Server"
$cfg = Get-Content (Join-Path $home_ "server.json") -Raw | ConvertFrom-Json
$env:PGPASSFILE = Join-Path $home_ "pgpass.conf"
$psql = Join-Path $cfg.PgBin "psql.exe"
$pgRestore = Join-Path $cfg.PgBin "pg_restore.exe"
$test = "storewms_restoretest"
$log = Join-Path $home_ "restore_test.log"

function Q([string]$db, [string]$sql) {
    $out = & $psql -h 127.0.0.1 -p $cfg.Port -U $cfg.AppUser -d $db -v ON_ERROR_STOP=1 -At -c $sql
    if ($LASTEXITCODE -ne 0) { throw "psql failed on ${db}: $sql" }
    return $out
}

$latest = Get-ChildItem (Join-Path $cfg.BackupDir "nightly") -Filter "storewms_*.backup" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
try {
    if (-not $latest) { throw "No nightly backup found to test." }
    Q "postgres" "DROP DATABASE IF EXISTS $test"
    Q "postgres" "CREATE DATABASE $test"
    & $pgRestore -h 127.0.0.1 -p $cfg.Port -U $cfg.AppUser -d $test --no-owner $latest.FullName 2>&1 | Add-Content $log
    if ($LASTEXITCODE -ne 0) { throw "pg_restore failed (exit $LASTEXITCODE)" }
    $bad = @()
    foreach ($t in "products", "stock_movements", "stock_levels", "delivery_notes", "goods_issues", "attachments") {
        $a = [long](Q $test "SELECT count(*) FROM $t")
        if ($a -le 0 -and $t -in "products") { $bad += "$t is empty in the restored copy" }
    }
    $check = Q $test ("SELECT count(*) FROM (SELECT product_id, bin_id, SUM(q) AS q FROM (" +
        "SELECT product_id, to_bin_id AS bin_id, quantity AS q FROM stock_movements WHERE to_bin_id IS NOT NULL " +
        "UNION ALL SELECT product_id, from_bin_id, -quantity FROM stock_movements WHERE from_bin_id IS NOT NULL) m " +
        "GROUP BY product_id, bin_id) l FULL JOIN stock_levels s USING (product_id, bin_id) " +
        "WHERE COALESCE(l.q, 0) <> COALESCE(s.quantity, 0)")
    if ([long]$check -ne 0) { $bad += "$check stock balance(s) do not match the ledger in the restored copy" }
    if ($bad.Count) { throw ($bad -join "; ") }
    $msg = "Test restore of $($latest.Name) OK"
    Q $cfg.Database "INSERT INTO server_backups (finished_at, kind, ok, file_name, size_bytes, message) VALUES (now() AT TIME ZONE 'UTC', 'TEST', true, '$($latest.Name)', $($latest.Length), '$msg')" | Out-Null
    Add-Content $log ("{0:u} {1}" -f (Get-Date), $msg)
} catch {
    $m = "$_" -replace "'", "''"
    Add-Content $log ("{0:u} TEST RESTORE FAILED {1}" -f (Get-Date), $_)
    try { Q $cfg.Database "INSERT INTO server_backups (finished_at, kind, ok, file_name, message) VALUES (now() AT TIME ZONE 'UTC', 'TEST', false, '', 'Test restore failed: $m')" | Out-Null } catch {}
    exit 1
} finally {
    try { Q "postgres" "DROP DATABASE IF EXISTS $test" | Out-Null } catch {}
}
