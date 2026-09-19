$ErrorActionPreference = 'SilentlyContinue'
# Kill voice-ai's server (matched by its venv path) + its whole process tree.
# NOTE: do NOT use a bare "server.py" match - the remote-dev-server also runs as
# "python server.py" and must be left alone.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'voice-ai.*server\.py' } | ForEach-Object { taskkill /PID $_.ProcessId /T /F | Out-Null }
# Kill the model subprocesses (DITTO, breeze, omnivoice, lux)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'ditto_server\.py|breeze_infer|omnivoice_server\.py|lux_server\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Write-Host "voice-ai stopped"
