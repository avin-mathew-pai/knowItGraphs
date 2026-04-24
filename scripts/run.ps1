# Windows PowerShell launcher.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path .env)) {
  Write-Host "[run.ps1] .env not found — copying .env.example -> .env"
  Copy-Item .env.example .env
  Write-Host "[run.ps1] Edit .env now and add your GEMINI_API_KEY (https://aistudio.google.com/apikey)"
  Write-Host "[run.ps1] Then re-run this script."
  exit 1
}

docker compose up --build -d
Write-Host ""
Write-Host "KnowIT-Graphs is starting. Open http://localhost:8080"
Write-Host "Follow logs: docker compose logs -f app"
