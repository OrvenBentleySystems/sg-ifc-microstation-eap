<#
.SYNOPSIS
    Packages the IFC+SG Checker for distribution to other MicroStation users.

.DESCRIPTION
    Produces a self-contained folder (and optional .zip) holding the tool, the
    IFC+SG library, the ribbon icon, the installer, and the GUI DGN library that
    carries the ribbon button. A colleague runs install_ifcsg_checker.ps1 from the
    unpacked folder and restarts MicroStation.

    Include the dgnlib once you have created the ribbon button in it: the button
    then travels with the package and nobody else has to touch the Customize
    dialog.

.PARAMETER OutputDir
    Where to build the package. Defaults to the user's Desktop.

.PARAMETER Source
    Installed tool folder to package. Defaults to C:\ProgramData\Bentley\IFCSG_Checker.

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

function Find-LiveInstall {
    # The .cfg is authoritative - it names the folder MicroStation actually loads,
    # which is not always the default install path.
    $cfg = Get-ChildItem "C:\ProgramData\Bentley" -Recurse -Filter "IFCSG_Checker.cfg" `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cfg) {
        $hit = Select-String -Path $cfg.FullName -Pattern '^\s*IFCSG_CHECKER_DIR\s*=\s*(.+?)\s*$' |
            Select-Object -First 1
        if ($hit) {
            $dir = $hit.Matches[0].Groups[1].Value.TrimEnd('\')
            if (Test-Path (Join-Path $dir "data\catalogue.json")) { return $dir }
        }
    }
    foreach ($guess in @("C:\ProgramData\Bentley\IFCSG_CheckerApp",
                         "C:\ProgramData\Bentley\IFCSG_Checker")) {
        if (Test-Path (Join-Path $guess "data\catalogue.json")) { return $guess }
    }
    return $null
}

if (-not $Source) { $Source = Find-LiveInstall }
if (-not $Source -or -not (Test-Path (Join-Path $Source "data\catalogue.json"))) {
    throw "No installed IFC+SG Checker found. Run install_ifcsg_checker.ps1 first, or pass -Source."
}

if (-not $DgnLib) {
    $candidate = Get-ChildItem "C:\ProgramData\Bentley" -Recurse -Filter "IFCSG_Checker.dgnlib" `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($candidate) { $DgnLib = $candidate.FullName }
}

$stamp = Get-Date -Format "yyyyMMdd"
$pkgName = "IFCSG_Checker_$stamp"
$pkg = Join-Path $OutputDir $pkgName

Write-Host "`nPackaging IFC+SG Checker" -ForegroundColor Cyan
Write-Step "source : $Source"
Write-Step "package: $pkg"

if (Test-Path $pkg) { Remove-Item $pkg -Recurse -Force }
$null = New-Item -ItemType Directory -Force -Path $pkg

$packageItems = @(
    "data",
    "docs",
    "schema",
    "tools\build_catalogue.py",
    "tools\microstation",
    "Deploy.bat",
    "README.md",
    "LICENSE",
    "NOTICE"
)
foreach ($relative in $packageItems) {
    $item = Join-Path $Source $relative
    if (-not (Test-Path $item)) { continue }
    $target = Join-Path $pkg $relative
    if (Test-Path $item -PathType Container) {
        $null = New-Item -ItemType Directory -Force -Path $target
        Copy-Item (Join-Path $item "*") $target -Recurse -Force
    } else {
        $null = New-Item -ItemType Directory -Force -Path (Split-Path $target -Parent)
        Copy-Item $item $target -Force
    }
}
Get-ChildItem $pkg -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $pkg "tools\microstation\tests") -Recurse -Force `
    -ErrorAction SilentlyContinue
# Exclude a legacy write-back prototype that may remain in an older overlay install.
# It guessed IFC classes and is not part of the audited checker package.
Get-ChildItem $pkg -Recurse -File -Filter "ifcsg_harvester.py" -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue
Write-Step "copied tool, library and icons"

$edition = "unknown"
$psets = "?"
$cataloguePath = Join-Path $pkg "data\catalogue.json"
if (Test-Path $cataloguePath) {
    try {
        $j = Get-Content $cataloguePath -Raw | ConvertFrom-Json
        if ($j.metadata.mapping_edition) { $edition = $j.metadata.mapping_edition }
        $psets = @($j.property_sets).Count
    } catch { }
}
Write-Step "library: mapping edition $edition, $psets SGPsets"

if ($DgnLib -and (Test-Path $DgnLib)) {
    $null = New-Item -ItemType Directory -Force -Path (Join-Path $pkg "dgnlib")
    Copy-Item $DgnLib (Join-Path $pkg "dgnlib\IFCSG_Checker.dgnlib") -Force
    Write-Step "included ribbon dgnlib from $DgnLib"
} else {
    Write-Warning "  No IFCSG_Checker.dgnlib found. The package will install the tool but"
    Write-Warning "  each user will have to create the ribbon button themselves."
}

$readme = @"
IFC+SG Checker for MicroStation
===============================

A CORENET X pre-flight checker. Reads an IFC file opened natively or attached as
a reference, evaluates the IFC+SG rule set, and reports which objects conform.
Selecting a finding selects the matching geometry in MicroStation.

This is an unofficial plugin for MicroStation created by Orven Fajardo. It is
not an official Bentley Systems plugin, product, or support offering. Bentley
Systems does not maintain or endorse it.

Bundled catalogue: mapping edition $edition, $psets property sets.

DEPLOY - 3 STEPS
----------------
1. Copy this whole folder to the target machine. Anywhere local is fine.

2. Right-click  Deploy.bat  and choose  Run as administrator.
   That installs the tool, the library, the MicroStation configuration and, if
   this package includes one, the ribbon button library.

3. Restart MicroStation. The configuration is only read at start-up.

HOW TO OPEN IT
--------------
Key-in (Utilities > Key-in, or press Enter twice):

    python load C:\ProgramData\Bentley\IFCSG_Checker\tools\microstation\run_ifcsg_checker.py

Type it once and it stays in the key-in history drop-down afterwards.

A ribbon button appears only if the dgnlib in this package was customised to
carry one. If there is no button, use the key-in - it works either way.

OPTIONAL - MAKE YOUR OWN BUTTON
-------------------------------
File > Settings > Configuration > Customize. Open

    <MicroStation Configuration>\Organization\Dgnlib\Gui\IFCSG_Checker.dgnlib

Add a Toolbox, then a Tool whose Key-in is the line above, and save. Then
File > Settings > User > Customize Ribbon to place it on a tab. Redistribute that
dgnlib and nobody else repeats this.

REQUIREMENTS
------------
MicroStation CONNECT / 2026 or OpenBuildings Designer. No Python installation and
no third-party packages are needed: the tool runs on MicroStation's embedded
Python and uses only the standard library.

IFC REFERENCE COMPATIBILITY
---------------------------
Deployment enables IFC_ALLOW_DEPRECATED_SCHEMA=1, the compatibility setting
documented in Bentley KB0098741. It allows trusted IFC files with deprecated
schema definitions to be opened, imported or referenced. It does not repair an
invalid IFC or make a legacy schema valid for IFC+SG submission.

To keep Bentley's default restriction, run install_ifcsg_checker.ps1 with
-DisableDeprecatedIfcSchemas instead of Deploy.bat.

MAPPING EDITION - READ THIS
---------------------------
The bundled catalogue was checked against the official BCA industry mapping
workbook. Its edition and source hashes are stamped in every report.

Before relying on this for a submission, rebuild the library from the current BCA
industry workbook:

    python tools\build_catalogue.py "industry-mapping-<date>.xlsx" ^
        --mapping-edition <date> --cop-edition <n>

The catalogue update runs outside MicroStation and uses only the Python standard
library. The checker UI does not modify its catalogue.

This is a pre-flight aid, not a compliance determination. The Qualified Person
remains responsible for reviewing code compliance.
"@
Set-Content -Path (Join-Path $pkg "README.txt") -Value $readme -Encoding UTF8
Write-Step "wrote README.txt"

$template = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) "Deploy.bat"
if (Test-Path $template) {
    Copy-Item $template (Join-Path $pkg "Deploy.bat") -Force
    Write-Step "wrote Deploy.bat"
} else {
    Write-Warning "  Deploy.bat missing; recipients must run the installer by hand."
}

if ($Zip) {
    $zipPath = "$pkg.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Compress-Archive -Path (Join-Path $pkg "*") -DestinationPath $zipPath
    Write-Step "wrote $zipPath ($([math]::Round((Get-Item $zipPath).Length/1MB,1)) MB)"
}

Write-Host "`nDone. Send the folder$(if($Zip){' or the .zip'}) to your colleagues." -ForegroundColor Green
