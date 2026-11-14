<#
Store WMS - one-off set-up of the shared database server (Windows 10/11 Pro).

Before running:
  1. Install PostgreSQL for Windows, version 16 or newer (EnterpriseDB installer, all defaults). Note the password
     you choose for the "postgres" superuser.
  2. Give this PC a fixed IP address in the router (DHCP reservation).
  3. Know the networks of every building, e.g. 192.168.1.0/24 (this office) and 192.168.2.0/24
     (the other building, reached through the VPN between the routers).

Run in PowerShell "as Administrator":
  powershell -ExecutionPolicy Bypass -File setup_server.ps1 -Networks "192.168.1.0/24,192.168.2.0/24" -BackupDir "D:\StoreWMS-Backups"

What it does (each step can be run again safely):
  - creates the database "storewms" and its user "storewms" with a long random password;
  - lets PostgreSQL listen on the network, but only accepts the listed networks (scram-sha-256);
  - opens port 5432 in Windows Firewall for those networks only (never the internet);
  - stops the PC sleeping or hibernating;
  - schedules the backups (server\backup.ps1): hourly 08:00-18:00 Mon-Sat, nightly 22:00, weekly
    copy on Sunday, and a monthly test restore (server\restore_test.ps1).
At the end it prints the password to type into "Connection..." on every PC. Keep it safe.
#>
param(
    [Parameter(Mandatory = $true)][string]$Networks,
    [Parameter(Mandatory = $true)][string]$BackupDir,
    [string]$OffsiteDir = "",
    [string]$PgVersion = "",
    [int]$Port = 5432,
    [string]$Database = "storewms",
    [string]$AppUser = "storewms"
)
$ErrorActionPreference = "Stop"

if (-not $PgVersion) {  # the newest PostgreSQL installed
    $PgVersion = (Get-ChildItem "C:\Program Files\PostgreSQL" -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^\d+$' } | Sort-Object { [int]$_.Name } | Select-Object -Last 1).Name
}
$pgRoot = "C:\Program Files\PostgreSQL\$PgVersion"
$psql = Join-Path $pgRoot "bin\psql.exe"
$dataDir = Join-Path $pgRoot "data"
$service = "postgresql-x64-$PgVersion"
$home_ = Join-Path $env:ProgramData "StoreWMS-Server"
if (-not (Test-Path $psql)) { throw "PostgreSQL $PgVersion was not found in $pgRoot. Install it first." }
New-Item -ItemType Directory -Force -Path $home_, $BackupDir | Out-Null

$nets = $Networks.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ }
foreach ($n in $nets) { if ($n -notmatch '^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$') { throw "Not a network: '$n' (use e.g. 192.168.2.0/24)" } }

Write-Host "Password of the PostgreSQL superuser 'postgres' (chosen when installing):"
$sec = Read-Host -AsSecureString
$env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))

function Sql([string]$sql, [string]$db = "postgres") {
    $out = & $psql -h 127.0.0.1 -p $Port -U postgres -d $db -v ON_ERROR_STOP=1 -At -c $sql
    if ($LASTEXITCODE -ne 0) { throw "psql failed: $sql" }
    return $out
}

# 1. user and database (a new random password only if the user does not exist yet)
$passFile = Join-Path $home_ "pgpass.conf"
$exists = Sql "SELECT 1 FROM pg_roles WHERE rolname = '$AppUser'"
if ($exists -ne "1") {
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $appPass = ([Convert]::ToBase64String($bytes)) -replace '[+/=]', 'x'
    Sql "CREATE ROLE $AppUser LOGIN PASSWORD '$appPass'"
    Set-Content -Path $passFile -Value "*:${Port}:*:${AppUser}:$appPass" -Encoding ASCII
} elseif (Test-Path $passFile) {
    $appPass = (Get-Content $passFile).Split(":")[-1]
} else {
    throw "User $AppUser exists but $passFile is missing. Reset its password in pgAdmin and write the file again."
}
# only Administrators and SYSTEM may read the password file
icacls $passFile /inheritance:r /grant:r "Administrators:F" "SYSTEM:F" | Out-Null
if ((Sql "SELECT 1 FROM pg_database WHERE datname = '$Database'") -ne "1") {
    Sql "CREATE DATABASE $Database OWNER $AppUser ENCODING 'UTF8' TEMPLATE template0"
}
Sql "REVOKE ALL ON DATABASE $Database FROM PUBLIC"
Sql "GRANT CONNECT ON DATABASE $Database TO $AppUser"
Sql "ALTER ROLE $AppUser CREATEDB"  # the monthly test restore puts a copy into a temporary database

# 2. network access: listen on the network, accept only the listed networks
Sql "ALTER SYSTEM SET listen_addresses = '*'"
Sql "ALTER SYSTEM SET password_encryption = 'scram-sha-256'"
Sql "ALTER SYSTEM SET max_connections = 200"
Sql "ALTER SYSTEM SET idle_in_transaction_session_timeout = '5min'"
$hba = Join-Path $dataDir "pg_hba.conf"
$lines = Get-Content $hba
$add = @()
foreach ($n in $nets) {
    $line = "host    $Database    $AppUser    $n    scram-sha-256    # Store WMS"
    if ($lines -notcontains $line) { $add += $line }
}
if ($add.Count) { Add-Content -Path $hba -Value $add -Encoding ASCII }
Restart-Service $service
Start-Sleep -Seconds 3

# 3. firewall: port 5432 from the listed networks only
$rule = "Store WMS database (PostgreSQL)"
Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $rule -Direction Inbound -Protocol TCP -LocalPort $Port -RemoteAddress $nets -Action Allow | Out-Null

# 4. the server must not sleep
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /hibernate off

# 5. scheduled backups (run as SYSTEM, whether anyone is logged in or not)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Copy-Item (Join-Path $here "backup.ps1"), (Join-Path $here "restore_test.ps1") $home_ -Force
$cfg = @{ BackupDir = $BackupDir; OffsiteDir = $OffsiteDir; Port = $Port; Database = $Database; AppUser = $AppUser; PgBin = (Join-Path $pgRoot "bin") }
$cfg | ConvertTo-Json | Set-Content (Join-Path $home_ "server.json") -Encoding UTF8
function Task([string]$name, [string]$script, [string]$kind, $trigger) {
    $act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$home_\$script`" -Kind $kind"
    $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask -TaskName "Store WMS $name" -Action $act -Trigger $trigger -Settings $set -User "SYSTEM" -RunLevel Highest -Force | Out-Null
}
$days = "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"
$hourly = @(); foreach ($h in 8..18) { $hourly += New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At ("{0:00}:00" -f $h) }
Task "hourly backup" "backup.ps1" "HOURLY" $hourly
Task "nightly backup" "backup.ps1" "NIGHTLY" (New-ScheduledTaskTrigger -Daily -At "22:00")
Task "weekly backup" "backup.ps1" "WEEKLY" (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "23:00")
Task "monthly test restore" "restore_test.ps1" "TEST" (New-ScheduledTaskTrigger -Weekly -WeeksInterval 4 -DaysOfWeek Sunday -At "06:00")

$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.PrefixOrigin -ne "WellKnown" } | Select-Object -First 1).IPAddress
Write-Host ""
Write-Host "Done. On every PC: sign-in screen > Connection... > Shared server" -ForegroundColor Green
Write-Host "  Server:        $ip"
Write-Host "  Port:          $Port"
Write-Host "  Database:      $Database"
Write-Host "  Database user: $AppUser"
Write-Host "  Password:      $appPass"
Write-Host "Keep this password safe (it is also in $passFile, readable by administrators only)."
Write-Host "Next: on the PC that has the data now, Settings > Move data to the shared server..."
