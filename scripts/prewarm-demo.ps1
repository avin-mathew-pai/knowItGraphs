# Windows PowerShell version — pre-warm the response cache before a demo.
$ErrorActionPreference = "Stop"
$API = if ($env:API) { $env:API } else { "http://localhost:8080" }
$Headers = @{ "Content-Type" = "application/json" }
if ($env:AGENT_API_TOKEN) {
    $Headers["Authorization"] = "Bearer $($env:AGENT_API_TOKEN)"
}

$Questions = @(
    "How does disambiguation work at the inter level?",
    "Can I disable a candidateKey in disambiguation? If yes, show the format.",
    "What is the schema for the output block in the inventory config?",
    "How do I integrate a new entity source into the KG pipeline?",
    "What does exceptionFilter do in candidateKeys?",
    "Why is my Spark job failing with OutOfMemoryError during shuffle?",
    "How do I reduce latency in a disambiguation pipeline?",
    "What is the difference between intra-level and inter-level disambiguation?"
)

Write-Host "Pre-warming cache for $($Questions.Count) questions against $API ..."
foreach ($q in $Questions) {
    Write-Host ""
    Write-Host "-> $q"
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $body = @{ question = $q } | ConvertTo-Json
    try {
        $resp = Invoke-RestMethod -Method Post -Uri "$API/api/agent/ask" -Headers $Headers -Body $body
        $sw.Stop()
        Write-Host "   done in $([int]$sw.Elapsed.TotalSeconds)s (cached=$($resp.cached))"
    } catch {
        $sw.Stop()
        Write-Host "   FAILED after $([int]$sw.Elapsed.TotalSeconds)s: $_"
    }
}

Write-Host ""
Write-Host "Cache stats:"
Invoke-RestMethod -Uri "$API/api/cache" -Headers $Headers | ConvertTo-Json
Write-Host "Pre-warm complete."
