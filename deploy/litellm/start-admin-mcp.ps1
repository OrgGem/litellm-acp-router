# Launched by Codex over stdio. Never write secrets or banners to stdout.
$ErrorActionPreference = 'Stop'
$envPath = Join-Path $PSScriptRoot '.env.mcp'
if (-not (Test-Path -LiteralPath $envPath)) {
    throw 'Missing .env.mcp. Create a personal proxy_admin key and configure this file.'
}
foreach ($line in [System.IO.File]::ReadAllLines($envPath)) {
    if ($line -match '^([A-Z_][A-Z0-9_]*)=(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
    }
}
$uvx = (Get-Command uvx -ErrorAction Stop).Source
& $uvx --isolated --python 3.12 --from 'git+https://github.com/BerriAI/litellm-admin-mcp.git@3d4cd40225430f32fee061f42a1fa538ae8d01b2' litellm-admin-mcp
exit $LASTEXITCODE
