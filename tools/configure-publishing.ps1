param(
    [string]$Repository = "marinfrankovic/Unifi-NOC",
    [string]$Namespace = "mfrankovic"
)
$ErrorActionPreference = "Stop"
$configured = $false
try {
    $raw = "https://index.docker.io/v1/" | docker-credential-desktop.exe get
    if ($LASTEXITCODE -ne 0) { throw "Docker Desktop credential access failed" }
    $credential = $raw | ConvertFrom-Json
    if ($credential.Username -ne $Namespace) { throw "Docker Hub account does not match requested namespace" }
    $body = @{ identifier = $credential.Username; secret = $credential.Secret } | ConvertTo-Json -Compress
    $auth = Invoke-RestMethod -Method Post -Uri "https://hub.docker.com/v2/auth/token" -ContentType "application/json" -Body $body
    $headers = @{ Authorization = "Bearer $($auth.access_token)" }
    $env:DOCKERHUB_USERNAME = $Namespace
    $env:DOCKERHUB_TOKEN = $credential.Secret
    node (Join-Path $PSScriptRoot "hub-metadata.mjs")
    if ($LASTEXITCODE -ne 0) { throw "Docker Hub repository initialization failed" }
    $request = @{
        token_label = "Unifi-NOC GitHub Actions $([DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss'))"
        scopes = @("repo:write")
        expires_at = [DateTime]::UtcNow.AddYears(1).ToString("o")
    } | ConvertTo-Json -Compress
    $token = Invoke-RestMethod -Method Post -Uri "https://hub.docker.com/v2/access-tokens" -Headers $headers -ContentType "application/json" -Body $request
    if (-not $token.token) { throw "Docker Hub did not return a dedicated CI token" }
    $token.token | gh secret set DOCKERHUB_TOKEN --repo $Repository
    if ($LASTEXITCODE -ne 0) { throw "GitHub encrypted secret configuration failed" }
    gh variable set DOCKERHUB_USERNAME --repo $Repository --body $Namespace
    if ($LASTEXITCODE -ne 0) { throw "GitHub namespace variable configuration failed" }
    $configured = $true
    $metadataRequest = @{
        token_label = "Unifi-NOC metadata $([DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss'))"
        scopes = @("repo:admin")
        expires_at = [DateTime]::UtcNow.AddYears(1).ToString("o")
    } | ConvertTo-Json -Compress
    $metadataToken = Invoke-RestMethod -Method Post -Uri "https://hub.docker.com/v2/access-tokens" -Headers $headers -ContentType "application/json" -Body $metadataRequest
    $metadataToken.token | gh secret set DOCKERHUB_METADATA_TOKEN --repo $Repository
    if ($LASTEXITCODE -ne 0) { throw "GitHub metadata secret configuration failed" }
    $env:DOCKERHUB_USERNAME = $Namespace
    $env:DOCKERHUB_TOKEN = $metadataToken.token
    node (Join-Path $PSScriptRoot "hub-metadata.mjs")
    if ($LASTEXITCODE -ne 0) { throw "Docker Hub metadata initialization failed" }
    Write-Output "Dedicated Docker Hub write token stored in GitHub encrypted secrets; expiry $($token.expires_at)."
} catch {
    throw "Publishing setup failed: $($_.Exception.Message)"
} finally {
    if (-not $configured -and $token.uuid -and $headers) {
        Invoke-RestMethod -Method Delete -Uri "https://hub.docker.com/v2/access-tokens/$($token.uuid)" -Headers $headers | Out-Null
    }
    Remove-Item Env:DOCKERHUB_TOKEN -ErrorAction SilentlyContinue
    Remove-Variable raw,credential,body,auth,headers,request,token,metadataRequest,metadataToken -ErrorAction SilentlyContinue
}