$ErrorActionPreference = 'SilentlyContinue'

# 1. Stop the app server - whatever is listening on 8900 - plus its process tree.
#    Killing by port is robust regardless of how the server was launched (venv
#    launcher, full path, or a different install location). Do NOT match a bare
#    "server.py" - the remote-dev-server also runs as "python server.py".
$serverPid = (Get-NetTCPConnection -LocalPort 8900 -State Listen -ErrorAction SilentlyContinue).OwningProcess
if ($serverPid) {
    taskkill /PID $serverPid /T /F | Out-Null
}

# 2. Stop the model subprocesses (DITTO, breeze, omnivoice, lux + idle worker).
#    These are spawned by the app server and may be orphaned if it was killed.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -match 'ditto_server\.py|breeze_infer|omnivoice_server\.py|lux_server\.py|gen_idle_worker\.py'
} | ForEach-Object {
    taskkill /PID $_.ProcessId /T /F 2>$null | Out-Null
}

Write-Host "voice-ai stopped"
