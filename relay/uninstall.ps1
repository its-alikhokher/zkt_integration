# Removes the ZKT relay from this PC. Run as Administrator:
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1
#
# FIRST point the attendance device back to the server it fed before (biotime_url in
# config.json), or that server stops receiving:
#   device menu: COMM. > Cloud Server Setting > Server Address / Server Port

$Task = "ZKT Relay"
Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*zkt_relay.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Get-NetFirewallRule -DisplayName "ZKT Relay *" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Write-Host "Relay stopped and removed (task + firewall rule)."
Write-Host "The folder C:\zkt_relay is kept (log + any punches not yet sent to ERPNext). Delete it by hand when no longer needed."
