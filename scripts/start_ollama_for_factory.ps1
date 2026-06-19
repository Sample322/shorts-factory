$ErrorActionPreference = "Stop"

$models = "C:\ollama_models"
New-Item -ItemType Directory -Force -Path $models | Out-Null
[Environment]::SetEnvironmentVariable("OLLAMA_MODELS", $models, "User")
$env:OLLAMA_MODELS = $models
$env:OLLAMA_HOST = "http://127.0.0.1:11434"

Get-CimInstance Win32_Process |
    Where-Object {
        $_.ProcessId -ne $PID -and
        $_.Name -in @("ollama.exe", "ollama app.exe", "llama-server.exe")
    } |
    ForEach-Object {
        try {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
            Write-Host "Stopped $($_.Name) pid=$($_.ProcessId)"
        } catch {
            Write-Host "Skip pid=$($_.ProcessId): $($_.Exception.Message)"
        }
    }

Start-Sleep -Seconds 2

$ollama = (Get-Command ollama).Source
$psi = [System.Diagnostics.ProcessStartInfo]::new()
$psi.FileName = $ollama
$psi.Arguments = "serve"
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.EnvironmentVariables["OLLAMA_MODELS"] = $models
$psi.EnvironmentVariables["OLLAMA_HOST"] = "http://127.0.0.1:11434"
$process = [System.Diagnostics.Process]::Start($psi)

Start-Sleep -Seconds 4
Write-Host "Started ollama serve pid=$($process.Id)"
Write-Host "OLLAMA_MODELS=$models"
ollama list
