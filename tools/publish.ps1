param([string]$Message = "Update Unifi-NOC")
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
if (-not (Test-Path (Join-Path $root ".git"))) { throw "An isolated Git checkout is required" }
Push-Location $root
try {
    node tools/privacy-check.mjs --self-test
    if ($LASTEXITCODE -ne 0) { throw "Privacy validation failed" }
    npm --prefix frontend run build
    if ($LASTEXITCODE -ne 0) { throw "Frontend validation failed" }
    git add -- .
    if ($LASTEXITCODE -ne 0) { throw "Staging failed" }
    node tools/privacy-check.mjs
    if ($LASTEXITCODE -ne 0) { throw "Staged publication validation failed" }
    git diff --cached --quiet
    if ($LASTEXITCODE -eq 1) {
        git commit -m $Message
        if ($LASTEXITCODE -ne 0) { throw "Commit failed" }
    } elseif ($LASTEXITCODE -ne 0) { throw "Git change check failed" }
    git push origin main
    if ($LASTEXITCODE -ne 0) { throw "GitHub push failed" }
    Write-Output "Published reviewed local changes. GitHub Actions updates Pages and versioned images."
} finally { Pop-Location }