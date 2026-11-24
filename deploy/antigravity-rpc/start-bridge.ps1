$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$state = Join-Path $repo '.local/antigravity-rpc/bridge'
$settings = Join-Path $state 'settings.json'
$keyFile = Join-Path $state 'bridge-key'
$python = Join-Path $repo '.venv/Scripts/python.exe'
$bridge = Join-Path $PSScriptRoot 'bridge.py'

New-Item -ItemType Directory -Force -Path $state | Out-Null
if (-not (Test-Path -LiteralPath $keyFile -PathType Leaf)) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $secret = 'sk-agy-bridge-' + ([BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant())
    [System.IO.File]::WriteAllText($keyFile, $secret)
}
if (-not (Test-Path -LiteralPath $settings -PathType Leaf)) {
    $cfg = @{ port = 14004; concurrency = 4 } | ConvertTo-Json
    [System.IO.File]::WriteAllText($settings, $cfg)
}

$listener = Get-NetTCPConnection -State Listen -LocalPort 14004 -ErrorAction SilentlyContinue
if ($listener) {
    $pidFile = Join-Path $state 'bridge.pid'
    if ((Test-Path -LiteralPath $pidFile) -and $listener.OwningProcess -eq [int](Get-Content -LiteralPath $pidFile)) {
        Write-Output "Antigravity bridge already listening (PID $($listener.OwningProcess))."
        exit 0
    }
    throw 'Port 14004 is already in use.'
}

$proc = Start-Process -FilePath $python -ArgumentList @('-u', "`"$bridge`"", "`"$settings`"") `
    -WorkingDirectory $state -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $state 'bridge.out.log') `
    -RedirectStandardError (Join-Path $state 'bridge.err.log')

$deadline = [DateTime]::UtcNow.AddSeconds(15)
while ([DateTime]::UtcNow -lt $deadline) {
    if ($proc.HasExited) { throw "Antigravity bridge exited with code $($proc.ExitCode). Check bridge.err.log." }
    $listener = Get-NetTCPConnection -State Listen -LocalPort 14004 -ErrorAction SilentlyContinue
    if ($listener.LocalAddress -eq '127.0.0.1') {
        Set-Content -LiteralPath (Join-Path $state 'bridge.pid') -Value $listener.OwningProcess
        Write-Output "Antigravity bridge listening on 127.0.0.1:14004 (PID $($listener.OwningProcess))."
        exit 0
    }
    Start-Sleep -Milliseconds 250
}
throw 'Antigravity bridge did not listen within 15 seconds.'
