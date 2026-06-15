param([switch]$Restart)
$ErrorActionPreference = "Stop"

$Repo = "D:\code\ORAC"
$HostName = "127.0.0.1"
$Port = 8765
$Url = "http://$HostName`:$Port"

Set-Location $Repo
$env:PYTHONPATH = Join-Path $Repo "src"

function Test-OracServer {
    try {
        Invoke-WebRequest -Uri "$Url/api/resources" -UseBasicParsing -TimeoutSec 2 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Stop-Orac {
    # Stop ORAC's python services (the UI server, plus any chat run / whatsapp
    # bridge launched from this repo). The UI server owns the loop thread and its
    # connector subprocesses, so killing it stops those too.
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'orac\.cli' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    # The node WhatsApp bridge on port 8788 is orphaned when its python parent
    # dies and keeps holding the port, so a fresh bridge cannot bind. Clear it.
    $conn = Get-NetTCPConnection -LocalPort 8788 -State Listen -ErrorAction SilentlyContinue
    if ($conn) { Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 800
}

if ($Restart) {
    Write-Host "Restarting ORAC: stopping any running instance..."
    Stop-Orac
} elseif (Test-OracServer) {
    # Already up and we were not asked to restart: just bring the desk forward.
    Start-Process $Url
    exit 0
}

python -m orac.cli init | Out-Host
$Process = Start-Process -FilePath "python" -ArgumentList @("-m", "orac.cli", "ui", "--host", $HostName, "--port", "$Port") -WorkingDirectory $Repo -PassThru

for ($Attempt = 0; $Attempt -lt 45; $Attempt++) {
    if ($Process.HasExited) {
        Write-Host "ORAC exited before the UI became ready. Exit code: $($Process.ExitCode)"
        Read-Host "Press Enter to close"
        exit $Process.ExitCode
    }
    if (Test-OracServer) {
        Start-Process $Url
        exit 0
    }
    Start-Sleep -Seconds 1
}

Write-Host "ORAC is still starting. Try opening $Url in a moment."
Read-Host "Press Enter to close"
