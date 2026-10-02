<#
.SYNOPSIS
    Packages the IFC+SG Checker for distribution to other users.

.DESCRIPTION
    Produces a self-contained folder (and optional .zip) holding the checker,
    every COP catalogue, Deploy.bat and Uninstall.bat. Recipients unzip it,
    right-click Deploy.bat > Run as administrator, and restart MicroStation.

    If the ribbon button was created in the installed GUI DGN library, that
    library travels with the package and nobody else repeats the Customize steps.

.PARAMETER OutputDir
    Where to build the package. Defaults to the user's Desktop.

.PARAMETER Source
    Installed tool folder or repository to package. Defaults to the live install.

.PARAMETER DgnLib
    GUI DGN library containing the ribbon button. Auto-detected when omitted.

.PARAMETER Zip
    Also produce a .zip alongside the folder.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File share_ifcsg_checker.ps1 -Zip
#>

[CmdletBinding()]
param(
    [string]$OutputDir,
    [string]$Source,
    [string]$DgnLib,
    [switch]$Zip
)

$ErrorActionPreference = "Stop"

if (-not $OutputDir) {
    # USERPROFILE\Desktop is a stale stub when Desktop is redirected to OneDrive.
    $OutputDir = [Environment]::GetFolderPath('Desktop')
    if (-not $OutputDir) { $OutputDir = Join-Path $env:USERPROFILE "Desktop" }
}

function Write-Step($m) { Write-Host "  $m" }

function Test-CheckerRoot($dir) {
    return $dir -and (Test-Path (Join-Path $dir "data\catalogues"))
}

function Find-LiveInstall {
    # A registered .cfg names the folder MicroStation actually loads.
    $roots = @("$env:ProgramFiles\Bentley", "$env:ProgramData\Bentley") | Where-Object { Test-Path $_ }
    foreach ($root in $roots) {
        $cfgs = Get-ChildItem $root -Recurse -Depth 5 -Filter "IFCSG_Checker.cfg" -ErrorAction SilentlyContinue
        foreach ($cfg in $cfgs) {
            $hit = Select-String -Path $cfg.FullName -Pattern '^\s*IFCSG_CHECKER_DIR\s*=\s*(.+?)\s*$' |
                Select-Object -First 1
            if ($hit) {
                $dir = $hit.Matches[0].Groups[1].Value.TrimEnd('\')
                if (Test-CheckerRoot $dir) { return $dir }
            }
        }
    }
    $default = "C:\ProgramData\Bentley\IFCSG_Checker"
    if (Test-CheckerRoot $default) { return $default }
    return $null
}

if (-not $Source) { $Source = Find-LiveInstall }
if (-not (Test-CheckerRoot $Source)) {
    throw "No IFC+SG Checker found. Run Deploy.bat first, or pass -Source <repository or install folder>."
}

if (-not $DgnLib) {
    $local = Join-Path $Source "dgnlib\IFCSG_Checker.dgnlib"
    if (Test-Path $local) { $DgnLib = $local }
}

$stamp = Get-Date -Format "yyyyMMdd"
$version = "unknown"
$init = Join-Path $Source "tools\microstation\ifcsg_checker\__init__.py"
if (Test-Path $init) {
    $match = Select-String -Path $init -Pattern '__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($match) { $version = $match.Matches[0].Groups[1].Value }
}
$pkgName = "IFCSG_Checker_${version}_$stamp"
$pkg = Join-Path $OutputDir $pkgName

Write-Host "`nPackaging IFC+SG Checker $version" -ForegroundColor Cyan
Write-Step "source : $Source"
Write-Step "package: $pkg"

if (Test-Path $pkg) { Remove-Item $pkg -Recurse -Force }
$null = New-Item -ItemType Directory -Force -Path $pkg

foreach ($relative in @(
    "data",
    "docs",
    "schema",
    "tools\build_catalogue.py",
    "tools\validate_catalogue.py",
    "tools\microstation",
    "Deploy.bat",
    "Uninstall.bat",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "NOTICE"
)) {
    $item = Join-Path $Source $relative
    if (-not (Test-Path $item)) { continue }
    $dest = Join-Path $pkg $relative
    if (Test-Path $item -PathType Container) {
        $null = New-Item -ItemType Directory -Force -Path $dest
        Copy-Item (Join-Path $item "*") $dest -Recurse -Force
    } else {
        $null = New-Item -ItemType Directory -Force -Path (Split-Path $dest -Parent)
        Copy-Item $item $dest -Force
    }
}
Get-ChildItem $pkg -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $pkg "tools\microstation\tests") -Recurse -Force -ErrorAction SilentlyContinue
foreach ($legacy in @("tools\microstation\ifcsg_harvester.py", "tools\microstation\install_ribbon_dgnlib.ps1",
                      "data\catalogue.json")) {
    Remove-Item (Join-Path $pkg $legacy) -Force -ErrorAction SilentlyContinue
}
foreach ($required in @("Deploy.bat", "Uninstall.bat", "tools\microstation\install_ifcsg_checker.ps1")) {
    if (-not (Test-Path (Join-Path $pkg $required))) { throw "Package is missing $required" }
}
Write-Step "copied checker and catalogues"

$editions = @()
foreach ($file in (Get-ChildItem (Join-Path $pkg "data\catalogues") -Filter "*.json")) {
    try {
        $meta = (Get-Content $file.FullName -Raw | ConvertFrom-Json).metadata
        $editions += "COP {0} (mapping {1})" -f $meta.cop_edition, $meta.mapping_edition
    } catch { }
}
$editionText = ($editions | Sort-Object -Descending) -join ", "
Write-Step "catalogues: $editionText"

if ($DgnLib -and (Test-Path $DgnLib)) {
    $null = New-Item -ItemType Directory -Force -Path (Join-Path $pkg "dgnlib")
    Copy-Item $DgnLib (Join-Path $pkg "dgnlib\IFCSG_Checker.dgnlib") -Force
    Write-Step "included ribbon library from $DgnLib"
} else {
    Write-Step "no ribbon library included; recipients use the key-in or Start menu"
}

$readme = @"
IFC+SG Checker $version for MicroStation
========================================

CORENET X pre-flight checker. Reads an IFC file opened or referenced in
MicroStation (or browsed from disk), checks it against the selected CORENET X
Code of Practice edition, and selects failing objects in MicroStation.

Unofficial tool by Orven Fajardo. Not a Bentley Systems product. Not the
official CORENET X Model Checker. The Qualified Person remains responsible.

Included COP editions: $editionText

INSTALL
-------
1. Unzip this folder anywhere on the machine.
2. Right-click Deploy.bat > Run as administrator.
3. Restart MicroStation.

Deploy.bat registers the checker with every MicroStation-based product it
finds: MicroStation CONNECT Edition, 2023, 2024, 2025, 2026 and PowerPlatform
products such as OpenBuildings, OpenRoads, OpenPlant and Descartes.

OPEN IT
-------
MicroStation 2024 or later - key-in:

    python load `$(IFCSG_CHECKER_LAUNCHER)

Any release, or no MicroStation at all - Start menu > IFC+SG Checker.
That window has the same checks and reports; use Browse IFC to pick a file.
MicroStation CONNECT Edition and 2023 have no MicroStation Python, so use the
Start menu route there. It uses the Python that ships with MicroStation 2024+,
or any Python 3.10+ with tcl/tk from python.org.

CHOOSE THE COP EDITION
----------------------
Use the Code of Practice drop-down at the top right of the checker window. The
newest edition is the default. An older edition is flagged as superseded in
the window and in every report.

UNINSTALL
---------
Right-click Uninstall.bat > Run as administrator.

NOTES
-----
Deploy.bat sets IFC_ALLOW_DEPRECATED_SCHEMA=1 (Bentley KB0098741) so trusted
legacy IFC files can be referenced. To keep Bentley's default, run:

    powershell -ExecutionPolicy Bypass -File tools\microstation\install_ifcsg_checker.ps1 -DisableDeprecatedIfcSchemas

Organisations that assign MS_GUIDGNLIBLIST with '=' in their own configuration
must add the line from IFCSG_Checker.cfg themselves to see the ribbon button.
"@
Set-Content -Path (Join-Path $pkg "README.txt") -Value $readme -Encoding UTF8
Write-Step "wrote README.txt"

if ($Zip) {
    $zipPath = "$pkg.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Compress-Archive -Path (Join-Path $pkg "*") -DestinationPath $zipPath
    Write-Step "wrote $zipPath ($([math]::Round((Get-Item $zipPath).Length/1MB,1)) MB)"
}

Write-Host "`nDone. Send the folder$(if($Zip){' or the .zip'}) to your colleagues." -ForegroundColor Green
