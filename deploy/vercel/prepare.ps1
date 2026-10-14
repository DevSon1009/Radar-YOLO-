$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$backendDir = Join-Path $projectRoot "backend"
$siteDir = Join-Path $PSScriptRoot "site"

New-Item -ItemType Directory -Path $siteDir -Force | Out-Null

Copy-Item (Join-Path $backendDir "dashboard.html") (Join-Path $siteDir "index.html") -Force
Copy-Item (Join-Path $backendDir "manifest.webmanifest") $siteDir -Force
Copy-Item (Join-Path $backendDir "icon.svg") $siteDir -Force
Copy-Item (Join-Path $backendDir "sw.js") $siteDir -Force
Copy-Item (Join-Path $PSScriptRoot "vercel.json") $siteDir -Force
Copy-Item (Join-Path $PSScriptRoot "package.json") $siteDir -Force
Copy-Item (Join-Path $PSScriptRoot "build.mjs") $siteDir -Force

Write-Output "Prepared static Vercel site at $siteDir"
