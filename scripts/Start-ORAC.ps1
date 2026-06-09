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

if (Test-OracServer) {
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
