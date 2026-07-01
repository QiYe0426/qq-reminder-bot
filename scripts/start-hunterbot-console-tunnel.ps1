param(
    [int]$LocalPort = 8090,
    [string]$RemoteHost = "127.0.0.1",
    [int]$RemotePort = 8080,
    [string]$Target = "tencent-bot"
)

$ErrorActionPreference = "Stop"

$created = $false
$mutex = New-Object System.Threading.Mutex($true, "Local\HunterbotConsoleTunnel", [ref]$created)
if (-not $created) {
    exit 0
}

$logDir = Join-Path $env:LOCALAPPDATA "Hunterbot\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir "console-tunnel.log"

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $logPath -Value $line
}

function Test-PortOpen {
    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $task = $client.BeginConnect("127.0.0.1", $LocalPort, $null, $null)
        if (-not $task.AsyncWaitHandle.WaitOne(1200, $false)) {
            $client.Close()
            return $false
        }
        $client.EndConnect($task)
        $client.Close()
        return $true
    } catch {
        return $false
    }
}

$ssh = (Get-Command ssh.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $ssh) {
    $ssh = "C:\Windows\System32\OpenSSH\ssh.exe"
}

$sshArgs = @(
    "-N",
    "-T",
    "-L", "$LocalPort`:$RemoteHost`:$RemotePort",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    $Target
)

Write-Log "Tunnel helper started."

try {
    while ($true) {
        if (Test-PortOpen) {
            Start-Sleep -Seconds 30
            continue
        }

        Write-Log "Starting SSH tunnel to $Target ($LocalPort -> $RemoteHost`:$RemotePort)."
        & $ssh @sshArgs
        $code = $LASTEXITCODE
        Write-Log "SSH tunnel exited with code $code."
        Start-Sleep -Seconds 8
    }
}
finally {
    if ($mutex) {
        $mutex.ReleaseMutex() | Out-Null
        $mutex.Dispose()
    }
    Write-Log "Tunnel helper stopped."
}
