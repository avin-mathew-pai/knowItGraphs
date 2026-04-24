# demo-start.ps1 — Windows launcher for the full demo stack.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..\..")

if (-not (Test-Path .env)) {
    Write-Host "[demo] .env missing — run scripts\run.ps1 first to configure KnowIT."
    exit 1
}
if (-not (Test-Path demo\jars\app.jar)) {
    Write-Host "[demo] demo\jars\app.jar is missing. Place your sds-ei-analytics jar there."
    exit 1
}

if (-not $env:SDS_EI_REPO) {
    if (Test-Path "..\sds-solution-ei") {
        $env:SDS_EI_REPO = "..\sds-solution-ei"
    } elseif (Test-Path "..\..\sds-solution-ei") {
        $env:SDS_EI_REPO = "..\..\sds-solution-ei"
    } else {
        Write-Host "[demo] SDS_EI_REPO not set and sds-solution-ei not found."
        exit 1
    }
}
Write-Host "[demo] Using SDS_EI_REPO=$env:SDS_EI_REPO"

docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build

Write-Host ""
Write-Host "  KnowIT UI:     http://localhost:8080"
Write-Host "  Airflow UI:    http://localhost:8088   (admin / admin)"
Write-Host "  Spark master:  http://localhost:8090"
Write-Host "  MinIO console: http://localhost:9001   (minio / minio12345)"
