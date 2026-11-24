param(
    [Parameter(Mandatory)][string]$SandboxCli,
    [string]$Endpoint = $env:MINIPOD_ENDPOINT,
    [ValidateRange(60, 86400)][int]$LeaseSeconds = 7200,
    [switch]$Offline
)

$ErrorActionPreference = 'Stop'
$sandboxCliPath = (Get-Command $SandboxCli -CommandType Application).Source
$daemonArgs = @()
if ($Endpoint) { $daemonArgs = @('--endpoint', $Endpoint) }

$image = 'minipod-codex:0.167.0'
$container = 'codex'
$archive = [System.IO.Path]::GetTempFileName()
$sandboxId = $null
try {
    docker image save -o $archive $image
    if ($LASTEXITCODE -ne 0) { throw "Build $image before starting the sandbox." }

    $network = if ($Offline) {
        @('--network', 'isolated')
    } else {
        @('--network', 'allow:auth.openai.com:443', '--network', 'allow:chatgpt.com:443',
          '--network', 'allow:api.openai.com:443')
    }
    $lease = & $sandboxCliPath @daemonArgs --json spawn --isolation vm --accel preferred `
        --vcpus 2 --ram-mb 1536 --rss-mb 2048 --disk-mb 4096 --storage-mb 6144 `
        --ttl-secs $LeaseSeconds @network
    if ($LASTEXITCODE -ne 0) { throw 'Mini-pod could not start the VM.' }
    $sandboxId = ($lease | ConvertFrom-Json).id

    $dockerInfo = & $sandboxCliPath @daemonArgs --json docker-endpoint $sandboxId
    if ($LASTEXITCODE -ne 0) { throw 'Mini-pod Docker endpoint unavailable.' }
    $dockerEndpoint = ($dockerInfo | ConvertFrom-Json).uri

    docker --host $dockerEndpoint image load -i $archive | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Image import failed.' }
    docker --host $dockerEndpoint run -d --pull never --name $container --cap-drop ALL `
        --security-opt no-new-privileges $image | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Codex container failed to start.' }
    docker --host $dockerEndpoint exec $container codex --version | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Codex binary failed inside the container.' }

    [pscustomobject]@{ SandboxId = $sandboxId; DockerEndpoint = $dockerEndpoint; Container = $container }
} catch {
    if ($sandboxId) {
        & $sandboxCliPath @daemonArgs destroy $sandboxId | Out-Host
    }
    throw
} finally {
    Remove-Item -LiteralPath $archive -ErrorAction SilentlyContinue
}
