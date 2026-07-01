<#
.SYNOPSIS
    Download the pinned tdl release binary into tools/tdl/ (Windows).

.DESCRIPTION
    tdl (https://github.com/iyear/tdl) is the multi-threaded media downloader
    this exporter shells out to. It is AGPL-3.0 and NOT vendored into this repo
    (tools/tdl/ is gitignored). This script fetches the pinned upstream release
    for your platform, verifies its SHA-256 against the published checksums, and
    extracts tdl.exe into tools/tdl/.

    Re-run it any time; it overwrites tools/tdl/tdl.exe. Bump $Version to update.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\fetch_tdl.ps1
#>
[CmdletBinding()]
param(
    # Pinned tdl version. Keep in sync with scripts/fetch_tdl.sh.
    [string]$Version = "0.20.2"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# tools/tdl relative to this script (scripts/ -> repo root -> tools/tdl).
$RepoRoot = Split-Path -Parent $PSScriptRoot
$DestDir = Join-Path $RepoRoot "tools\tdl"
$DestExe = Join-Path $DestDir "tdl.exe"

switch ($env:PROCESSOR_ARCHITECTURE) {
    "AMD64" { $arch = "64bit" }
    "ARM64" { $arch = "arm64" }
    "x86"   { $arch = "32bit" }
    default { throw "Unsupported CPU architecture: $($env:PROCESSOR_ARCHITECTURE)" }
}

$asset = "tdl_Windows_$arch.zip"
$base = "https://github.com/iyear/tdl/releases/download/v$Version"
$assetUrl = "$base/$asset"
$sumsUrl = "$base/tdl_checksums.txt"

Write-Host "Fetching tdl v$Version ($asset)..."

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("tdl_" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
try {
    $zipPath = Join-Path $tmp $asset
    $sumsPath = Join-Path $tmp "tdl_checksums.txt"
    Invoke-WebRequest -Uri $assetUrl -OutFile $zipPath -UseBasicParsing
    Invoke-WebRequest -Uri $sumsUrl -OutFile $sumsPath -UseBasicParsing

    # Verify SHA-256 against the published checksums file (lines: "<hash>  <name>").
    $expected = $null
    foreach ($line in Get-Content $sumsPath) {
        $parts = $line -split '\s+', 2
        if ($parts.Count -eq 2 -and $parts[1].Trim() -eq $asset) {
            $expected = $parts[0].Trim().ToLower()
            break
        }
    }
    if (-not $expected) { throw "Checksum for $asset not found in tdl_checksums.txt" }
    $actual = (Get-FileHash -Path $zipPath -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $expected) {
        throw "SHA-256 mismatch for $asset`n  expected: $expected`n  actual:   $actual"
    }
    Write-Host "Checksum OK ($expected)"

    New-Item -ItemType Directory -Path $DestDir -Force | Out-Null
    $extract = Join-Path $tmp "extract"
    Expand-Archive -Path $zipPath -DestinationPath $extract -Force
    $exe = Get-ChildItem -Path $extract -Filter "tdl.exe" -Recurse | Select-Object -First 1
    if (-not $exe) { throw "tdl.exe not found inside $asset" }
    Copy-Item -Path $exe.FullName -Destination $DestExe -Force
    Write-Host "Installed: $DestExe"
    & $DestExe version
}
finally {
    Remove-Item -Path $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
