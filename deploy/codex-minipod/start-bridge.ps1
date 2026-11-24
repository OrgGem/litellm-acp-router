param(
    [int]$Slots = 0,
    [double]$QueueTimeoutSeconds = -1,
    [int]$DiskMb = 0,
    [switch]$Restart
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$state = Join-Path $repo '.local/minipod-codex/bridge'
$settings = Join-Path $state 'settings.json'
$python = Join-Path $repo '.venv/Scripts/python.exe'
$bridge = Join-Path $PSScriptRoot 'bridge.py'
if (-not (Test-Path -LiteralPath $settings -PathType Leaf)) {
    throw "Missing bridge settings at $settings"
}

if ($Slots -gt 0) {
    $env:MINIPOD_SLOTS = [string]$Slots
}
if ($QueueTimeoutSeconds -ge 0) {
    $env:MINIPOD_QUEUE_TIMEOUT_SECONDS = [string]$QueueTimeoutSeconds
}
if ($DiskMb -gt 0) {
    $env:MINIPOD_DISK_MB = [string]$DiskMb
}

$pidFile = Join-Path $state 'bridge.pid'
$listener = Get-NetTCPConnection -State Listen -LocalPort 14002 -ErrorAction SilentlyContinue
if ($listener) {
    $recordedPid = if (Test-Path -LiteralPath $pidFile) { [int](Get-Content -LiteralPath $pidFile) } else { 0 }
    if ($listener.OwningProcess -eq $recordedPid) {
        if (-not $Restart) {
            Write-Output "Codex bridge already listening (PID $($listener.OwningProcess))."
            exit 0
        }
        & taskkill.exe /PID $listener.OwningProcess /T /F *> $null
        Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
        $stopDeadline = [DateTime]::UtcNow.AddSeconds(10)
        while ([DateTime]::UtcNow -lt $stopDeadline) {
            if (-not (Get-NetTCPConnection -State Listen -LocalPort 14002 -ErrorAction SilentlyContinue)) {
                break
            }
            Start-Sleep -Milliseconds 200
        }
    } else {
        throw 'Port 14002 is already in use.'
    }
}

$envPrefix = ''
if ($env:MINIPOD_SLOTS) {
    $envPrefix += "set MINIPOD_SLOTS=$($env:MINIPOD_SLOTS)&& "
}
if ($env:MINIPOD_QUEUE_TIMEOUT_SECONDS) {
    $envPrefix += "set MINIPOD_QUEUE_TIMEOUT_SECONDS=$($env:MINIPOD_QUEUE_TIMEOUT_SECONDS)&& "
}
if ($env:MINIPOD_DISK_MB) {
    $envPrefix += "set MINIPOD_DISK_MB=$($env:MINIPOD_DISK_MB)&& "
}

$outLog = Join-Path $state 'bridge.out.log'
$errLog = Join-Path $state 'bridge.err.log'
$cmdLine = "cmd.exe /c `"${envPrefix}`"$python`" -u `"$bridge`" `"$settings`" 1>`"$outLog`" 2>`"$errLog`"`""
$startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
$spawn = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = $cmdLine
    CurrentDirectory = $state
    ProcessStartupInformation = $startup
}
if ($spawn.ReturnValue -ne 0) {
    throw "Win32_Process.Create failed with code $($spawn.ReturnValue)"
}

$deadline = [DateTime]::UtcNow.AddSeconds(10)
while ([DateTime]::UtcNow -lt $deadline) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort 14002 -ErrorAction SilentlyContinue
    if ($listener.LocalAddress -eq '127.0.0.1') {
        Set-Content -LiteralPath $pidFile -Value $listener.OwningProcess
        $cfg = Get-Content -Raw -LiteralPath $settings | ConvertFrom-Json
        $activeSlots = if ($env:MINIPOD_SLOTS) { $env:MINIPOD_SLOTS } elseif ($cfg.slots) { $cfg.slots } else { '1' }
        $activeQueue = if ($env:MINIPOD_QUEUE_TIMEOUT_SECONDS) { $env:MINIPOD_QUEUE_TIMEOUT_SECONDS } elseif ($null -ne $cfg.queue_timeout_seconds) { $cfg.queue_timeout_seconds } else { '60' }
        Write-Output "Codex bridge listening on 127.0.0.1:14002 (PID $($listener.OwningProcess), slots=$activeSlots, queue_timeout=${activeQueue}s)."
        exit 0
    }
    Start-Sleep -Milliseconds 250
}
throw 'Codex bridge did not listen within 10 seconds.'
