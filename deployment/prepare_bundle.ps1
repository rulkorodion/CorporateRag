param(
    [string]$OutputPath = "",
    [string]$KnowledgeBaseOutputPath = "",
    [switch]$SkipKnowledgeBase,
    [switch]$SkipRuntimeData
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $OutputPath) {
    $OutputPath = Join-Path $ProjectRoot "tmp\corporate-rag-stage14-viewer.tar.gz"
}
$OutputPath = [System.IO.Path]::GetFullPath($OutputPath)
if (-not $KnowledgeBaseOutputPath) {
    $KnowledgeBaseOutputPath = Join-Path $ProjectRoot "tmp\corporate-rag-knowledge-base.tar.gz"
}
$KnowledgeBaseOutputPath = [System.IO.Path]::GetFullPath($KnowledgeBaseOutputPath)
$TempRoot = [System.IO.Path]::GetTempPath()
$Stage = Join-Path $TempRoot ("corporate-rag-stage14-viewer-" + [guid]::NewGuid().ToString("N"))

try {
    $null = New-Item -ItemType Directory -Path $Stage
    $StagePaths = @("app", "config")
    if (-not $SkipRuntimeData) {
        $StagePaths += @("storage\chunks", "storage\normalized", "storage\assets")
    }
    foreach ($path in $StagePaths) {
        $null = New-Item -ItemType Directory -Path (Join-Path $Stage $path) -Force
    }

    foreach ($directory in @("src", "tools", "deployment", "config")) {
        Copy-Item -LiteralPath (Join-Path $ProjectRoot $directory) `
            -Destination (Join-Path $Stage "app") -Recurse
    }
    Get-ChildItem -LiteralPath (Join-Path $Stage "app") `
        -Directory -Filter "__pycache__" -Recurse |
        Remove-Item -Recurse -Force
    $FrontendStage = Join-Path $Stage "app\frontend"
    $null = New-Item -ItemType Directory -Path $FrontendStage -Force
    foreach ($directory in @("public", "scripts", "src")) {
        Copy-Item -LiteralPath (Join-Path $ProjectRoot "frontend\$directory") `
            -Destination $FrontendStage -Recurse
    }
    Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "frontend") -File -Force |
        Where-Object { $_.Name -notin @(".env.local", "tsconfig.tsbuildinfo") } |
        Copy-Item -Destination $FrontendStage
    foreach ($file in @(
        ".dockerignore", "pyproject.toml", "README.md",
        "requirements-production.txt", "requirements-update.txt"
    )) {
        Copy-Item -LiteralPath (Join-Path $ProjectRoot $file) `
            -Destination (Join-Path $Stage "app")
    }

    if (-not $SkipRuntimeData) {
        foreach ($directory in @("chunks", "normalized", "assets")) {
            Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "storage\$directory") -Force |
                Copy-Item -Destination (Join-Path $Stage "storage\$directory") -Recurse
        }
    }

    $KnowledgeBase = Get-ChildItem -LiteralPath $ProjectRoot -Directory |
        Where-Object {
            (Get-ChildItem -LiteralPath $_.FullName -Recurse -File -ErrorAction SilentlyContinue |
                Where-Object { $_.Extension -in @(".pdf", ".docx", ".xlsx", ".mp4") } |
                Select-Object -First 100).Count -ge 100
        } |
        Select-Object -First 1
    if (-not $KnowledgeBase) {
        throw "Knowledge-base directory was not found under $ProjectRoot"
    }
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "deployment\production.env.example") `
        -Destination (Join-Path $Stage "config\production.env")

    $OutputDirectory = Split-Path -Parent $OutputPath
    $null = New-Item -ItemType Directory -Path $OutputDirectory -Force
    & tar.exe -czf $OutputPath -C $Stage .
    if ($LASTEXITCODE -ne 0) {
        throw "tar.exe failed with exit code $LASTEXITCODE"
    }
    $Archive = Get-Item -LiteralPath $OutputPath
    Write-Host "Bundle: $($Archive.FullName)"
    Write-Host "Size MB: $([math]::Round($Archive.Length / 1MB, 2))"

    if (-not $SkipKnowledgeBase) {
        $KnowledgeBaseOutputDirectory = Split-Path -Parent $KnowledgeBaseOutputPath
        $null = New-Item -ItemType Directory -Path $KnowledgeBaseOutputDirectory -Force
        & tar.exe -czf $KnowledgeBaseOutputPath -C $KnowledgeBase.FullName .
        if ($LASTEXITCODE -ne 0) {
            throw "knowledge-base tar.exe failed with exit code $LASTEXITCODE"
        }
        $KnowledgeArchive = Get-Item -LiteralPath $KnowledgeBaseOutputPath
        Write-Host "Knowledge base: $($KnowledgeArchive.FullName)"
        Write-Host "Size MB: $([math]::Round($KnowledgeArchive.Length / 1MB, 2))"
    }
}
finally {
    if (Test-Path -LiteralPath $Stage) {
        $ResolvedStage = (Resolve-Path -LiteralPath $Stage).Path
        $ResolvedTemp = (Resolve-Path -LiteralPath $TempRoot).Path
        if ($ResolvedStage.StartsWith($ResolvedTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $ResolvedStage -Recurse -Force
        }
    }
}
