# Shorts Factory launcher with auto-cleanup
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 as
# Windows-1251 when there is no BOM, which corrupts Cyrillic text and
# breaks quote parsing.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Port = 8505

# 1. Kill anything listening on our port (old Streamlit)
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($conn) {
    $listenPids = $conn.OwningProcess | Sort-Object -Unique
    foreach ($targetPid in $listenPids) {
        Write-Host "Killing PID $targetPid (was holding port $Port)" -ForegroundColor Yellow
        Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}

# 2. Kill stale Streamlit pythons from previous runs
$stale = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'streamlit' }
foreach ($p in $stale) {
    Write-Host "Killing stale Streamlit PID $($p.ProcessId)" -ForegroundColor Yellow
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
if ($stale) { Start-Sleep -Seconds 1 }

# 3. Ensure logs directory exists
$logDir = Join-Path $PSScriptRoot "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

# 4. PATH for ffmpeg and venv
$env:Path = "$PSScriptRoot\.venv\Scripts;C:\ffmpeg\bin;$env:Path"

Write-Host "Starting Shorts Factory on http://localhost:$Port (background)" -ForegroundColor Green

# pythonw.exe + Start-Process -WindowStyle Hidden = fully detached process.
# Will NOT die when you close this PowerShell window.
# pythonw.exe + Start-Process -WindowStyle Hidden = fully detached process.
# Will NOT die when you close this PowerShell window.
$proc = Start-Process -FilePath ".\.venv\Scripts\pythonw.exe" `
    -ArgumentList "-m","streamlit","run","app.py",
                  "--browser.gatherUsageStats","false",
                  "--server.maxUploadSize","2048",
                  "--server.headless","true",
                  "--server.port",$Port `
    -WorkingDirectory $PSScriptRoot `
    -WindowStyle Hidden `
    -PassThru `
    -RedirectStandardOutput "$logDir\streamlit.out.log" `
    -RedirectStandardError "$logDir\streamlit.err.log"

Write-Host "Started PID $($proc.Id). Logs: $logDir\streamlit.out.log" -ForegroundColor Green

# Wait for port to start listening (max 25 sec)
for ($i = 1; $i -le 25; $i++) {
    Start-Sleep -Seconds 1
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        Write-Host "READY: http://localhost:$Port (took $i sec)" -ForegroundColor Green
        Write-Host ""
        Write-Host "To stop manually: Stop-Process -Id $($proc.Id) -Force" -ForegroundColor Gray
        Write-Host "Or just run this script again - it kills the old one first." -ForegroundColor Gray
        exit 0
    }
}

Write-Host "FAIL: port $Port did not open in 25 sec. See log: $logDir\streamlit.err.log" -ForegroundColor Red
exit 1
