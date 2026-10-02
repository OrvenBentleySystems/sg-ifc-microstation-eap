<#
.SYNOPSIS
    Installs the IFC+SG Checker for every MicroStation-based product on this machine.

.DESCRIPTION
    Copies the checker to a machine-wide folder and registers it with each
    detected MicroStation CONNECT Edition, MicroStation 2023/2024/2025/2026 and
    PowerPlatform vertical (OpenBuildings, OpenRoads, OpenPlant, Descartes, ...).

    Registration writes IFCSG_Checker.cfg into the product's own
    <install>\config\appl\ folder. msconfig.cfg includes that folder at
    Application level for every WorkSpace, including organisations that use a
    custom or managed configuration. A product whose msconfig.cfg does not
    include config\appl falls back to its ProgramData Organization folder.

    The GUI DGN library for the ribbon button lives in the install folder and is
    added through MS_GUIDGNLIBLIST, so a customised button survives reinstalls
    and is shared by every product.

    A standalone launcher (IFCSG_Checker.cmd plus a Start menu shortcut) runs
    the full checker window without MicroStation. That is the route for
    releases older than MicroStation 2024, which have no MicroStation Python.

.PARAMETER Target
    Install folder. Defaults to C:\ProgramData\Bentley\IFCSG_Checker.

.PARAMETER ProductRoot
    One or more product program folders (the folder holding config\msconfig.cfg).
    Auto-detected when omitted.

.PARAMETER ListProducts
    List detected products and exit without changing anything.

.PARAMETER Uninstall
    Remove the checker, its configuration from every product and the shortcut.

.PARAMETER DisableDeprecatedIfcSchemas
    Do not enable Bentley's IFC_ALLOW_DEPRECATED_SCHEMA compatibility setting.

.PARAMETER NoShortcut
    Do not create or remove the Start menu shortcut.

.PARAMETER ProgramDataRoot
    Bentley ProgramData folder scanned for legacy Organization registrations.
    Defaults to %ProgramData%\Bentley.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install_ifcsg_checker.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install_ifcsg_checker.ps1 -ListProducts
#>

[CmdletBinding()]
param(
    [string]$Target = "C:\ProgramData\Bentley\IFCSG_Checker",
    [string[]]$ProductRoot,
    [switch]$ListProducts,
    [switch]$Uninstall,
    [switch]$DisableDeprecatedIfcSchemas,
    [switch]$NoShortcut,
    [string]$ProgramDataRoot = (Join-Path $env:ProgramData "Bentley")
)

$ErrorActionPreference = "Stop"
$CfgName = "IFCSG_Checker.cfg"
$DgnLibName = "IFCSG_Checker.dgnlib"
$CfgMarker = "IFCSG_CHECKER_DIR"
$ShortcutPath = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\IFC+SG Checker.lnk"


function Write-Step($msg) { Write-Host "  $msg" }
function Write-Head($msg) { Write-Host "`n$msg" -ForegroundColor Cyan }


function Test-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}


function Get-ProductInfo([string]$msdir) {
    $msdir = $msdir.TrimEnd('\')
    $msconfig = Join-Path $msdir "config\msconfig.cfg"
    if (-not (Test-Path $msconfig)) { return $null }
    $text = Get-Content $msconfig -Raw
    $usesAppl = $text -match '(?im)^\s*%include\s+\$\(_USTN_APPL\)\*\.cfg' -or
                $text -match '(?im)^\s*%include\s+\$\(MSDIR\)config[\\/]appl[\\/]\*\.cfg'
    $version = ""
    foreach ($dll in @("ustation.dll", "microstation.exe", "ustation.exe")) {
        $file = Join-Path $msdir $dll
        if (Test-Path $file) {
            $version = (Get-Item $file).VersionInfo.FileVersion
            if ($version) { break }
        }
    }
    $name = Split-Path (Split-Path $msdir -Parent) -Leaf
    if ($name -eq "Bentley" -or -not $name) { $name = Split-Path $msdir -Leaf }
    $hasPython = [bool](Get-ChildItem $msdir -Filter "MSPyBentley*.pyd" -ErrorAction SilentlyContinue |
                        Select-Object -First 1)
    return [pscustomobject]@{
        Name      = $name
        Root      = $msdir
        Version   = $version
        UsesAppl  = $usesAppl
        HasPython = $hasPython
    }
}


function Find-Products([switch]$Auto) {
    if ($ProductRoot -and -not $Auto) {
        $out = @()
        foreach ($root in $ProductRoot) {
            $info = Get-ProductInfo $root
            if (-not $info) { throw "No config\msconfig.cfg under $root" }
            $out += $info
        }
        return $out
    }
    $roots = New-Object System.Collections.Generic.List[string]
    foreach ($base in @("$env:ProgramFiles\Bentley", "${env:ProgramFiles(x86)}\Bentley")) {
        if ($base -and (Test-Path $base)) { $roots.Add($base) }
    }
    # Products installed outside Program Files (for example D:\Bentley) are found
    # through their Windows uninstall entries.
    $uninstall = @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*")
    foreach ($entry in (Get-ItemProperty $uninstall -ErrorAction SilentlyContinue)) {
        $location = $entry.InstallLocation
        if ($entry.Publisher -like "Bentley*" -and $location -and (Test-Path $location)) {
            $roots.Add($location)
        }
    }
    $seen = @{}
    $out = @()
    foreach ($root in $roots) {
        $hits = Get-ChildItem $root -Recurse -Depth 3 -Filter "msconfig.cfg" -ErrorAction SilentlyContinue |
            Where-Object { $_.Directory.Name -eq "config" }
        foreach ($hit in $hits) {
            $msdir = $hit.Directory.Parent.FullName
            $key = $msdir.ToLowerInvariant()
            if ($seen.ContainsKey($key)) { continue }
            $seen[$key] = $true
            $info = Get-ProductInfo $msdir
            if ($info) { $out += $info }
        }
    }
    return $out
}


function Get-OrganizationFolders {
    $out = @()
    foreach ($dir in (Get-ChildItem $ProgramDataRoot -Directory -ErrorAction SilentlyContinue)) {
        $org = Join-Path $dir.FullName "Configuration\Organization"
        if (Test-Path $org) { $out += $org }
    }
    return $out
}


function Get-CfgPath($product) {
    if ($product.UsesAppl) { return Join-Path $product.Root "config\appl\$CfgName" }
    $org = Get-OrganizationFolders | Where-Object {
        (Get-OrgProductName $_) -eq $product.Name
    } | Select-Object -First 1
    if ($org) { return Join-Path $org $CfgName }
    return $null
}


function Test-OurCfg($path) {
    return (Test-Path $path -PathType Leaf) -and
        ((Get-Content $path -Raw) -match [regex]::Escape($CfgMarker))
}


function Test-TargetStillRegistered {
    <# True when any product outside this run's scope still points at $Target. #>
    $pattern = '(?m)^\s*IFCSG_CHECKER_DIR\s*=\s*' +
        [regex]::Escape($Target.TrimEnd('\')) + '\\?\s*$'
    $paths = @()
    foreach ($p in (Find-Products -Auto)) {
        $c = Get-CfgPath $p
        if ($c) { $paths += $c }
    }
    foreach ($org in Get-OrganizationFolders) { $paths += (Join-Path $org $CfgName) }
    foreach ($c in $paths) {
        if ((Test-Path $c -PathType Leaf) -and ((Get-Content $c -Raw) -match $pattern)) {
            return $true
        }
    }
    return $false
}


function Copy-Tree($src, $dst) {
    <#
      Copy over the top rather than deleting first. A failed delete used to abort
      the install with the target half removed, and files written by an elevated
      session cannot be deleted by a normal one.
    #>
    $null = New-Item -ItemType Directory -Force -Path $dst
    $blocked = @()
    foreach ($item in Get-ChildItem $src -Recurse) {
        $relative = $item.FullName.Substring($src.Length).TrimStart('\')
        if ($skip | Where-Object { $relative -like "*$_*" }) { continue }
        $dest = Join-Path $dst $relative
        if ($item.PSIsContainer) {
            $null = New-Item -ItemType Directory -Force -Path $dest
            continue
        }
        try {
            Copy-Item $item.FullName $dest -Force -ErrorAction Stop
        } catch {
            $blocked += $relative
        }
    }
    return $blocked
}


function New-ToolIcon($iconPath, $bmpPath) {
    Add-Type -AssemblyName System.Drawing
    $size = 32
    $bmp = New-Object System.Drawing.Bitmap $size, $size
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = "AntiAlias"
    $g.TextRenderingHint = "AntiAliasGridFit"
    $g.Clear([System.Drawing.Color]::Transparent)

    $bg = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 11, 107, 203))
    $g.FillEllipse($bg, 1, 1, $size - 2, $size - 2)

    $white = [System.Drawing.Brushes]::White
    $font = New-Object System.Drawing.Font "Segoe UI", 11, ([System.Drawing.FontStyle]::Bold),
        ([System.Drawing.GraphicsUnit]::Pixel)
    $fmt = New-Object System.Drawing.StringFormat
    $fmt.Alignment = "Center"
    $fmt.LineAlignment = "Center"
    $g.DrawString("SG", $font, $white, (New-Object System.Drawing.RectangleF 0, 3, $size, 16), $fmt)

    # tick mark under the letters, so the icon still reads at 16 px
    $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::White), 3
    $g.DrawLines($pen, @(
        (New-Object System.Drawing.Point 10, 22),
        (New-Object System.Drawing.Point 14, 26),
        (New-Object System.Drawing.Point 22, 18)))

    $g.Dispose()
    $bmp.Save($bmpPath, [System.Drawing.Imaging.ImageFormat]::Bmp)

    $hicon = $bmp.GetHicon()
    $icon = [System.Drawing.Icon]::FromHandle($hicon)
    $fs = [System.IO.File]::Create($iconPath)
    $icon.Save($fs)
    $fs.Close()
    $icon.Dispose()
    $bmp.Dispose()
}


function Find-SeedDgnLib($products) {
    foreach ($p in $products) {
        $direct = Join-Path $p.Root "Default\Data\PersonalDgnlibSeed.dgnlib"
        if (Test-Path $direct) { return $direct }
    }
    foreach ($p in $products) {
        $hit = Get-ChildItem $p.Root -Recurse -Filter "PersonalDgnlibSeed.dgnlib" -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}


function Get-OrgProductName($org) {
    return Split-Path (Split-Path $org -Parent) -Parent | Split-Path -Leaf
}


function Remove-LegacyRegistration($keepDgnlibAt, $scope) {
    <#
      Versions up to 1.3.x wrote to <ProgramData product>\Configuration\Organization.
      $scope limits the clean-up to those products; $null means every product.
    #>
    $names = $null
    if ($null -ne $scope) { $names = @($scope | ForEach-Object { $_.Name }) }
    foreach ($org in Get-OrganizationFolders) {
        if ($null -ne $names -and $names -notcontains (Get-OrgProductName $org)) { continue }
        $cfg = Join-Path $org $CfgName
        if (Test-OurCfg $cfg) {
            Remove-Item $cfg -Force
            Write-Step "removed legacy $cfg"
        }
        $lib = Join-Path $org "Dgnlib\Gui\$DgnLibName"
        if (Test-Path $lib) {
            if ($keepDgnlibAt -and -not (Test-Path $keepDgnlibAt)) {
                $null = New-Item -ItemType Directory -Force -Path (Split-Path $keepDgnlibAt -Parent)
                Copy-Item $lib $keepDgnlibAt -Force
                Write-Step "kept your ribbon library: $lib -> $keepDgnlibAt"
            }
            Remove-Item $lib -Force
            Write-Step "removed legacy $lib"
        }
    }
}


function Write-StandaloneLauncher($path) {
    $cmd = @'
@echo off
rem IFC+SG Checker standalone window. Works without MicroStation.
setlocal
set "HERE=%~dp0"
set "PY="
set "PYARGS="
if exist "%ProgramData%\Bentley\PowerPlatformPython\python\pythonw.exe" set "PY=%ProgramData%\Bentley\PowerPlatformPython\python\pythonw.exe"
if not defined PY for /f "delims=" %%P in ('where pyw.exe 2^>nul') do if not defined PY (set "PY=%%P" & set "PYARGS=-3")
if not defined PY for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY (
  echo.
  echo No Python 3 found.
  echo MicroStation 2024 or later installs one automatically.
  echo Otherwise install Python 3.10+ from https://www.python.org with tcl/tk enabled.
  echo.
  pause
  exit /b 1
)
start "" "%PY%" %PYARGS% "%HERE%tools\microstation\run_ifcsg_checker.py" --nodock %*
'@
    Set-Content -Path $path -Value $cmd -Encoding ASCII
}


function New-Shortcut($lnk, $targetPath, $icon, $workdir) {
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($lnk)
    $sc.TargetPath = $targetPath
    $sc.WorkingDirectory = $workdir
    $sc.IconLocation = "$icon,0"
    $sc.WindowStyle = 7
    $sc.Description = "IFC+SG / CORENET X pre-flight checker"
    $sc.Save()
}


# ---------------------------------------------------------------------------

$sourceRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$products = @(Find-Products)
$dgnLibPath = Join-Path $Target "dgnlib\$DgnLibName"

Write-Head "IFC+SG Checker installer"
Write-Step "source : $sourceRoot"
Write-Step "target : $Target"
Write-Head "Detected MicroStation-based products"
if ($products.Count -eq 0) {
    Write-Step "(none)"
} else {
    foreach ($p in $products) {
        $py = if ($p.HasPython) { "MicroStation Python: yes" } else { "MicroStation Python: no - use the standalone launcher" }
        $hook = if ($p.UsesAppl) { "config\appl" } else { "Organization" }
        Write-Step ("{0} {1}" -f $p.Name, $p.Version)
        Write-Step ("    {0}  |  {1}  |  {2}" -f $p.Root, $hook, $py)
    }
}
if ($ListProducts) { return }

if ($Uninstall) {
    Write-Head "Uninstalling"
    foreach ($p in $products) {
        $cfg = Get-CfgPath $p
        if ($cfg -and (Test-OurCfg $cfg)) {
            Remove-Item $cfg -Force
            Write-Step "removed $cfg"
        }
    }
    $scope = $null
    if ($ProductRoot) { $scope = $products }
    Remove-LegacyRegistration $null $scope
    if ($ProductRoot -and (Test-TargetStillRegistered)) {
        Write-Step "kept $Target - other products are still registered to it"
        Write-Host "`nDone. Restart MicroStation." -ForegroundColor Green
        return
    }
    $removeList = @($Target)
    if (-not $NoShortcut) { $removeList += $ShortcutPath }
    foreach ($path in $removeList) {
        if (Test-Path $path) {
            Remove-Item $path -Recurse -Force
            Write-Step "removed $path"
        }
    }
    Write-Host "`nDone. Restart MicroStation." -ForegroundColor Green
    return
}

if (-not (Test-Path (Join-Path $sourceRoot "data\catalogues"))) {
    throw "Source does not contain data\catalogues: $sourceRoot"
}

Write-Head "1. Copying tool and catalogues"
$null = New-Item -ItemType Directory -Force -Path $Target
$skip = @("__pycache__", ".lumina_upload_sessions", ".git")
$blocked = @()
foreach ($rel in @(
    "data",
    "docs",
    "schema",
    "dgnlib",
    "tools\build_catalogue.py",
    "tools\validate_catalogue.py",
    "tools\reconcile_cop_pdf.py",
    "tools\microstation",
    "Deploy.bat",
    "Uninstall.bat",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "NOTICE"
)) {
    $src = Join-Path $sourceRoot $rel
    if (-not (Test-Path $src)) { continue }
    $dst = Join-Path $Target $rel
    if ([IO.Path]::GetFullPath($src) -eq [IO.Path]::GetFullPath($dst)) { continue }
    if (Test-Path $src -PathType Container) {
        $blocked += Copy-Tree $src $dst
    } else {
        $null = New-Item -ItemType Directory -Force -Path (Split-Path $dst -Parent)
        try { Copy-Item $src $dst -Force -ErrorAction Stop } catch { $blocked += $rel }
    }
    Write-Step "copied $rel"
}

if ($blocked.Count -gt 0) {
    Write-Host ""
    Write-Warning "$($blocked.Count) file(s) could not be overwritten:"
    foreach ($b in ($blocked | Select-Object -First 8)) { Write-Host "      $b" }
    if ($blocked.Count -gt 8) { Write-Host "      ... and $($blocked.Count - 8) more" }
    Write-Host ""
    if (-not (Test-Elevated)) {
        Write-Host @"
Those files were written by an elevated session, so this one cannot replace them.
Nothing was deleted - the existing install is still intact and usable, just not
updated. Right-click Deploy.bat and choose Run as administrator.

"@ -ForegroundColor Yellow
    }
    throw "Install incomplete: $($blocked.Count) file(s) not updated."
}

# Overlay installs preserve files that were removed from the source tree. Clean
# only known obsolete/generated paths so an older deployment cannot leak them
# into the launcher or a shared package.
foreach ($relative in @(
    "data\catalogue.json",
    "data\00_context.json",
    "data\01_governance.json",
    "data\02_gateways_agencies.json",
    "data\03_sgpset_seed.json",
    "data\04_space_areas_ura.json",
    "data\05_entity_catalogue.json",
    "data\06_validation_rules.json",
    "data\07_mcp_tool_contracts.json",
    "data\08_bentley_binding.json",
    "tools\build_library.py",
    "tools\microstation\deploy_template.bat",
    "tools\microstation\fixture_report.csv",
    "tools\microstation\fixture_report.html",
    "tools\microstation\fixture_report.json",
    "tools\microstation\fixture_report.txt",
    "tools\microstation\ifcsg_harvester.py",
    "tools\microstation\mcp_preflight.ps1",
    "tools\microstation\install_ribbon_dgnlib.ps1",
    "tools\microstation\ifcsg_checker\librarybuild.py"
)) {
    $stale = Join-Path $Target $relative
    if (Test-Path $stale -PathType Leaf) {
        Remove-Item $stale -Force
        Write-Step "removed obsolete $relative"
    }
}
foreach ($relative in @("data\generated", "data\source", "tools\staad", "tools\microstation\tests")) {
    $stale = Join-Path $Target $relative
    if (Test-Path $stale -PathType Container) {
        Remove-Item $stale -Recurse -Force
        Write-Step "removed obsolete $relative"
    }
}
foreach ($relative in @("data\.lumina_upload_sessions", "tools\.lumina_upload_sessions")) {
    $stale = Join-Path $Target $relative
    if ((Test-Path $stale -PathType Container) -and
        @(Get-ChildItem $stale -Force).Count -eq 0) {
        Remove-Item $stale -Force
        Write-Step "removed empty $relative"
    }
}

Write-Head "2. Icon, ribbon library and standalone launcher"
$iconDir = Join-Path $Target "icons"
$null = New-Item -ItemType Directory -Force -Path $iconDir
$iconPath = Join-Path $iconDir "IFCSG_Checker.ico"
$bmpPath = Join-Path $iconDir "IFCSG_Checker.bmp"
New-ToolIcon $iconPath $bmpPath
Write-Step "wrote $iconPath"

Remove-LegacyRegistration $dgnLibPath $products
if (Test-Path $dgnLibPath) {
    Write-Step "ribbon library kept: $dgnLibPath"
} else {
    $seed = Find-SeedDgnLib $products
    if ($seed) {
        $null = New-Item -ItemType Directory -Force -Path (Split-Path $dgnLibPath -Parent)
        Copy-Item $seed $dgnLibPath -Force
        Write-Step "created ribbon library from seed: $dgnLibPath"
    } else {
        Write-Step "no seed DGN library found; the key-in still works"
    }
}

$launcherCmd = Join-Path $Target "IFCSG_Checker.cmd"
Write-StandaloneLauncher $launcherCmd
Write-Step "wrote $launcherCmd"
if (-not $NoShortcut) {
    try {
        New-Shortcut $ShortcutPath $launcherCmd $iconPath $Target
        Write-Step "Start menu: IFC+SG Checker"
    } catch {
        Write-Warning "  Could not create the Start menu shortcut: $_"
    }
}

Write-Head "3. Registering with MicroStation"
$launcher = Join-Path $Target "tools\microstation\run_ifcsg_checker.py"
$deprecatedIfcSetting = if ($DisableDeprecatedIfcSchemas) {
    @"
# Deprecated IFC schema compatibility is disabled.
# To enable Bentley KB0098741 compatibility, reinstall without
# -DisableDeprecatedIfcSchemas.
"@
} else {
    @"
# Bentley KB0098741 compatibility for opening, importing, and referencing
# trusted IFC files that use deprecated schema definitions.
# This broadens accepted IFC input. It does not repair or validate an IFC file.
IFC_ALLOW_DEPRECATED_SCHEMA = 1
"@
}
$cfg = @"
#----------------------------------------------------------------------
# IFCSG_Checker.cfg - IFC+SG / CORENET X pre-flight checker
#
# Written by install_ifcsg_checker.ps1. msconfig.cfg includes this folder
# for every WorkSpace, including custom and managed configurations.
#
# Key-in:
#   python load `$(IFCSG_CHECKER_LAUNCHER)
#----------------------------------------------------------------------

IFCSG_CHECKER_DIR       = $Target\
IFCSG_CHECKER_LAUNCHER  = $launcher
IFCSG_CHECKER_ICONS     = $Target\icons\

# Make the checker importable by any MicroStation Python script.
MS_PYTHONPATH           > `$(IFCSG_CHECKER_DIR)tools/microstation/

# Ribbon button library. Customise it once with File > Settings >
# Configuration > Customize; the button then appears in every product.
%if exists (`$(IFCSG_CHECKER_DIR)dgnlib/$DgnLibName)
MS_GUIDGNLIBLIST        > `$(IFCSG_CHECKER_DIR)dgnlib/$DgnLibName
%endif

$deprecatedIfcSetting
"@

$registered = 0
$failed = @()
foreach ($p in $products) {
    $cfgPath = Get-CfgPath $p
    if (-not $cfgPath) {
        $failed += "$($p.Name): no config\appl include and no Organization folder"
        continue
    }
    try {
        $null = New-Item -ItemType Directory -Force -Path (Split-Path $cfgPath -Parent)
        Set-Content -Path $cfgPath -Value $cfg -Encoding ASCII -ErrorAction Stop
        Write-Step "registered $($p.Name): $cfgPath"
        $registered++
    } catch {
        $failed += "$($p.Name): $($_.Exception.Message)"
    }
}
foreach ($f in $failed) { Write-Warning "  not registered - $f" }
if ($DisableDeprecatedIfcSchemas) {
    Write-Step "deprecated IFC schema compatibility: disabled"
} else {
    Write-Step "deprecated IFC schema compatibility: enabled (Bentley KB0098741)"
}
if ($products.Count -gt 0 -and $registered -eq 0) {
    if (-not (Test-Elevated)) {
        throw "Could not register with any product. Right-click Deploy.bat and choose Run as administrator."
    }
    throw "Could not register with any product."
}

$withPython = @($products | Where-Object { $_.HasPython }).Count
Write-Head "Installed."
Write-Host @"

  In MicroStation 2024 or later (restart MicroStation first):
      key-in:  python load `$(IFCSG_CHECKER_LAUNCHER)

  Without MicroStation, or in CONNECT Edition / 2023:
      Start menu > IFC+SG Checker      (or run $launcherCmd)

  Optional ribbon button, once per organisation:
      1. File > Settings > Configuration > Customize, open
             $dgnLibPath
      2. Tools tab: New Toolbox 'IFC+SG', New Tool 'IFC+SG Checker'
             Key-in: python load `$(IFCSG_CHECKER_LAUNCHER)
             Icon:   $iconPath
      3. Save. File > Settings > User > Customize Ribbon, add the tool.
      Run share_ifcsg_checker.ps1 afterwards; the package then carries the button.

"@ -ForegroundColor Green
if ($products.Count -eq 0) {
    Write-Warning "No MicroStation-based product was found. The standalone launcher is installed."
} elseif ($withPython -eq 0) {
    Write-Warning "None of the detected products has MicroStation Python (2024+). Use the standalone launcher."
}
