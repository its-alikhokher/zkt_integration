# Installs the ZKT relay on this PC. Run from this folder, as Administrator:
#   powershell -ExecutionPolicy Bypass -File install.ps1
# Safe to run again: installs this folder's zkt_relay.py and config.json; a different
# config.json already installed is saved as config.json.bak-<date> first. Queue and
# archive are kept.

$ErrorActionPreference = "Stop"
$Here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Target = "C:\zkt_relay"
$PyVer  = "3.12.8"
$Task   = "ZKT Relay"

if (-not (Test-Path "$Here\config.json")) {
    throw "config.json is missing. Copy config.example.json to config.json and fill it in."
}
$Config = Get-Content "$Here\config.json" -Raw | ConvertFrom-Json
$Port   = if ($Config.listen_port) { [int]$Config.listen_port } else { 8000 }
$Device = $Config.device_ip
if (-not $Device) {
    throw "Set device_ip in config.json: only the attendance device may reach the relay's port."
}

Write-Host "1/6 Checking port $Port..."
if (Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $Task
    Start-Sleep -Seconds 2
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $Port is already used by another program on this PC. Nothing was changed."
}

Write-Host "2/6 Copying the program to $Target..."
New-Item -ItemType Directory -Force -Path $Target | Out-Null
Copy-Item "$Here\zkt_relay.py" $Target -Force
if ((Test-Path "$Target\config.json") -and
    ((Get-FileHash "$Target\config.json").Hash -ne (Get-FileHash "$Here\config.json").Hash)) {
    $backup = "$Target\config.json.bak-" + (Get-Date -Format "yyyyMMdd-HHmmss")
    Copy-Item "$Target\config.json" $backup
    Write-Host "    previous config.json saved as $backup"
}
Copy-Item "$Here\config.json" $Target -Force

Write-Host "3/6 Python (a private copy in $Target\python, nothing installed system-wide)..."
if (-not (Test-Path "$Target\python\pythonw.exe")) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $zip = "$env:TEMP\python-$PyVer-embed-amd64.zip"
    Invoke-WebRequest -UseBasicParsing -Uri "https://www.python.org/ftp/python/$PyVer/python-$PyVer-embed-amd64.zip" -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath "$Target\python" -Force
    Remove-Item $zip
}
& "$Target\python\python.exe" -c "import ssl, http.server, urllib.request; print('    python ok')"
if ($LASTEXITCODE -ne 0) { throw "Python check failed." }

Write-Host "4/6 Firewall: port $Port open only for the device $Device..."
Get-NetFirewallRule -DisplayName "ZKT Relay $Port" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "ZKT Relay $Port" -Direction Inbound -Protocol TCP -LocalPort $Port `
    -RemoteAddress $Device -Action Allow -Profile Any | Out-Null

Write-Host "5/6 Background task (starts with Windows, restarts if it stops)..."
$action    = New-ScheduledTaskAction -Execute "$Target\python\pythonw.exe" -Argument "`"$Target\zkt_relay.py`"" -WorkingDirectory $Target
$triggers  = @(
    (New-ScheduledTaskTrigger -AtStartup),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5))
)
$settings  = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $Task -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $Task

Write-Host "6/6 Checking the relay answers..."
Start-Sleep -Seconds 3
try {
    Invoke-RestMethod "http://localhost:$Port/health" | Format-List
    Write-Host "Relay is running." -ForegroundColor Green
} catch {
    Write-Host "Relay did not answer. Look at $Target\relay.log" -ForegroundColor Red
}
