$ErrorActionPreference = "Stop"

$downloads = "C:\Users\Rodion\Downloads"
$destination = Join-Path $PSScriptRoot "..\public\icons"
New-Item -ItemType Directory -Force -Path $destination | Out-Null

$icons = @{
    "send.png" = "send.png"
    "icon_rag.png" = "knowledge-base.png"
    "jpg.png" = "file-jpg.png"
    "png.png" = "file-png.png"
    "xls.png" = "file-xls.png"
    "doc.png" = "file-doc.png"
    "pdf.png" = "file-pdf.png"
    "icon_ktz.png" = "knowledge-base-ktz.png"
    "send (1).png" = "send-accent.png"
    "free-icon-file-3143340.png" = "file-video.png"
}

foreach ($entry in $icons.GetEnumerator()) {
    Copy-Item -LiteralPath (Join-Path $downloads $entry.Key) `
        -Destination (Join-Path $destination $entry.Value) -Force
}

Get-ChildItem $destination | Select-Object Name, Length
