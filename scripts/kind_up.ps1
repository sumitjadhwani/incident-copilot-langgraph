# Create the kind cluster for the live demo and deploy everything into it.
#
#   .\scripts\kind_up.ps1            # cluster + images + manifests + buggy releases
#   .\scripts\kind_up.ps1 -Reset     # re-apply healthy revision 1, then the buggy revision 2 (fresh demo)
#
# Needs: Docker Desktop, kind, kubectl. Keycloak (docker compose) must be up for the MCP servers' JWKS.
param([switch]$Reset)
# Native tools write progress to stderr; PowerShell 5.1 would treat that as an error under "Stop".
# Exit codes are checked explicitly in Invoke-Checked instead.
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Cluster = "incident-copilot"
$Ctx = "kind-$Cluster"

function Find-Kind {
    $cmd = Get-Command kind -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $found = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter kind.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.FullName }
    throw "kind not found. Install with: winget install Kubernetes.kind"
}
function Invoke-Checked([string]$what, [scriptblock]$block) {
    Write-Host "==> $what" -ForegroundColor Cyan
    & $block
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" }
}
$Kind = Find-Kind
$K = @("--context", $Ctx)

function Deploy-Apps {
    Invoke-Checked "apply healthy revision 1" { kubectl @K apply -f "$Root/infra/kind/apps/acme-v1.yaml" -f "$Root/infra/kind/apps/globex.yaml" }
    foreach ($d in "checkout-api", "notification-svc") {
        Invoke-Checked "wait for $d rev 1" { kubectl @K -n acme-prod rollout status "deployment/$d" --timeout=180s }
    }
    Invoke-Checked "apply buggy revision 2" { kubectl @K apply -f "$Root/infra/kind/apps/acme-v2-bugs.yaml" }
}

if ($Reset) {
    Invoke-Checked "delete acme apps" { kubectl @K -n acme-prod delete deployment checkout-api notification-svc --ignore-not-found }
    Deploy-Apps
    Write-Host "Reset done: buggy releases are rolling out (crash loops start within a minute)." -ForegroundColor Green
    exit 0
}

$existing = & $Kind get clusters 2>$null
if ($existing -notcontains $Cluster) {
    $policy = (Resolve-Path "$Root/infra/kind/audit-policy.yaml").Path -replace '\\', '/'
    $config = Join-Path $env:TEMP "kind-incident-copilot.yaml"
    (Get-Content "$Root/infra/kind/cluster.yaml" -Raw).Replace("__AUDIT_POLICY__", $policy) | Set-Content -Encoding utf8 $config
    Invoke-Checked "create kind cluster" { & $Kind create cluster --config $config --wait 120s }
} else {
    Write-Host "==> cluster $Cluster exists" -ForegroundColor Cyan
}

Invoke-Checked "build buggy-service image" { docker build -q -t buggy-service:dev "$Root/buggy_service" }
Invoke-Checked "build MCP servers image" { docker build -q -t copilot-mcp:dev -f "$Root/mcp_servers/Dockerfile" $Root }
# Only the locally built images are side-loaded. Public images (Prometheus, kube-state-metrics, busybox)
# are pulled by the node itself: `kind load` fails on multi-platform images with Docker Desktop's
# containerd image store ("content digest ... not found").
foreach ($img in "buggy-service:dev", "copilot-mcp:dev") {
    Invoke-Checked "load $img" { & $Kind load docker-image $img --name $Cluster }
}

Invoke-Checked "apply namespaces, RBAC, monitoring, MCP servers" {
    kubectl @K apply -f "$Root/infra/kind/manifests/"
}
Invoke-Checked "restart MCP servers (pick up rebuilt image)" { kubectl @K -n copilot-system rollout restart deployment }
foreach ($d in "mcp-k8s-read", "mcp-k8s-write", "mcp-observability") {
    Invoke-Checked "wait for $d" { kubectl @K -n copilot-system rollout status "deployment/$d" --timeout=180s }
}
Invoke-Checked "wait for prometheus" { kubectl @K -n monitoring rollout status deployment/prometheus --timeout=180s }
Deploy-Apps

Write-Host ""
Write-Host "Cluster ready." -ForegroundColor Green
Write-Host "  MCP observability  http://localhost:30081/mcp"
Write-Host "  MCP k8s-read       http://localhost:30082/mcp"
Write-Host "  MCP k8s-write      http://localhost:30083/mcp"
Write-Host "  Prometheus         http://localhost:30090"
Write-Host "Point the API at the cluster:  `$env:COPILOT_TOOL_BACKEND='mcp'; docker compose -f infra/docker-compose.yml up -d api"
