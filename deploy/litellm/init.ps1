$ErrorActionPreference = 'Stop'
$envPath = Join-Path $PSScriptRoot '.env'
if (Test-Path -LiteralPath $envPath) {
    Write-Host 'Existing .env retained.'
    exit 0
}

function New-Secret {
    $bytes = [byte[]]::new(32)
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return -join ($bytes | ForEach-Object { $_.ToString('x2') })
}

$lines = @(
    'LITELLM_PORT=14000'
    'UI_USERNAME=admin'
    "UI_PASSWORD=$(New-Secret)"
    "LITELLM_MASTER_KEY=sk-$(New-Secret)"
    "LITELLM_SALT_KEY=$(New-Secret)"
    "POSTGRES_PASSWORD=$(New-Secret)"
    "REDIS_PASSWORD=$(New-Secret)"
)
[System.IO.File]::WriteAllLines($envPath, $lines)
Write-Host "Created $envPath. Keep this file private and back it up with the database."
