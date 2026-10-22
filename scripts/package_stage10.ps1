param(
    [string]$Output = "tmp/corporate-rag-stage10.tar.gz"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$outputPath = Join-Path $projectRoot $Output
$outputDirectory = Split-Path -Parent $outputPath
New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null

$paths = @(
    "src",
    "tools",
    "deployment",
    "frontend",
    "pyproject.toml",
    "README.md",
    "requirements-production.txt",
    "requirements-api.txt",
    "requirements-retrieval.txt"
)

Push-Location $projectRoot
try {
    tar --exclude="frontend/node_modules" `
        --exclude="frontend/.next" `
        --exclude="frontend/.env.local" `
        --exclude="frontend/coverage" `
        --exclude="frontend/tsconfig.tsbuildinfo" `
        --exclude="*/__pycache__" `
        --exclude="*.pyc" `
        -czf $outputPath @paths
}
finally {
    Pop-Location
}

Get-Item $outputPath | Select-Object FullName, Length, LastWriteTime
