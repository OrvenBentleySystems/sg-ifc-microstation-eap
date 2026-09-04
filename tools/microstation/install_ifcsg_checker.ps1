<#
.SYNOPSIS
    Installs the IFC+SG Checker as a permanent MicroStation tool.

.DESCRIPTION
    Copies the tool and its IFC+SG library to a machine-wide folder, writes a
    MicroStation configuration file, generates a ribbon icon, and creates an
    empty GUI DGN library for the ribbon button.

    Everything lands where MicroStation already looks:
      - Configuration\Organization\*.cfg      auto-included by msconfig.cfg
      - Configuration\Organization\Dgnlib\Gui already on MS_GUIDGNLIBLIST

    so the tool is available in every WorkSet, not just the current one.

    The one step this cannot do is create the ribbon button itself: MicroStation
    stores ribbon customisation inside a DGN library and only exposes it through
    the Customize dialog. The script prints the exact steps and the key-in.

.PARAMETER Target
    Install folder. Defaults to C:\ProgramData\Bentley\IFCSG_Checker.

.PARAMETER MicroStationConfig
    MicroStation Configuration folder. Auto-detected when omitted.

.PARAMETER Uninstall
    Remove the installed files, configuration and dgnlib.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install_ifcsg_checker.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install_ifcsg_checker.ps1 -Target "D:\CAD\IFCSG_Checker"
#>

[CmdletBinding()]
param(
    [string]$Target = "C:\ProgramData\Bentley\IFCSG_Checker",
    [string]$MicroStationConfig,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$CfgName = "IFCSG_Checker.cfg"
$DgnLibName = "IFCSG_Checker.dgnlib"


function Write-Step($msg) { Write-Host "  $msg" }
function Write-Head($msg) { Write-Host "`n$msg" -ForegroundColor Cyan }


function Find-MicroStationConfig {
    if ($MicroStationConfig) { return $MicroStationConfig }
    $candidates = Get-ChildItem "C:\ProgramData\Bentley" -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "MicroStation*" -or $_.Name -like "OpenBuildings*" } |
        Sort-Object Name -Descending
    foreach ($c in $candidates) {
        $cfg = Join-Path $c.FullName "Configuration"
        if (Test-Path (Join-Path $cfg "Organization")) { return $cfg }
    }
    throw "Could not find a MicroStation Configuration folder. Pass -MicroStationConfig explicitly."
}


function Find-SeedDgnLib {
    $roots = Get-ChildItem "C:\Program Files\Bentley" -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "MicroStation*" -or $_.Name -like "OpenBuildings*" }
    foreach ($r in $roots) {
        $hit = Get-ChildItem $r.FullName -Recurse -Filter "PersonalDgnlibSeed.dgnlib" -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($hit) { return $hit.FullName }
        $hit = Get-ChildItem $r.FullName -Recurse -Filter "seed3d.dgn" -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}


function Test-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
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
        $target = Join-Path $dst $relative
        if ($item.PSIsContainer) {
            $null = New-Item -ItemType Directory -Force -Path $target
            continue
        }
        try {
            Copy-Item $item.FullName $target -Force -ErrorAction Stop
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

    $bg = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 27, 107, 48))
    $g.FillRectangle($bg, 1, 1, $size - 2, $size - 2)

    $white = [System.Drawing.Brushes]::White
    $font = New-Object System.Drawing.Font "Segoe UI", 12, ([System.Drawing.FontStyle]::Bold),
        ([System.Drawing.GraphicsUnit]::Pixel)
    $fmt = New-Object System.Drawing.StringFormat
    $fmt.Alignment = "Center"
    $fmt.LineAlignment = "Center"
    $g.DrawString("SG", $font, $white, (New-Object System.Drawing.RectangleF 0, 2, $size, 18), $fmt)

    # tick mark under the letters, so the icon still reads at 16 px
    $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::White), 3
    $g.DrawLines($pen, @(
        (New-Object System.Drawing.Point 9, 23),
        (New-Object System.Drawing.Point 14, 28),
        (New-Object System.Drawing.Point 24, 17)))

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


# ---------------------------------------------------------------------------

$sourceRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$config = Find-MicroStationConfig
$orgDir = Join-Path $config "Organization"
$guiDir = Join-Path $orgDir "Dgnlib\Gui"
$cfgPath = Join-Path $orgDir $CfgName
$dgnLibPath = Join-Path $guiDir $DgnLibName

Write-Head "IFC+SG Checker installer"
Write-Step "source        : $sourceRoot"
Write-Step "target        : $Target"
Write-Step "configuration : $config"

if ($Uninstall) {
    Write-Head "Uninstalling"
    foreach ($p in @($cfgPath, $dgnLibPath, $Target)) {
        if (Test-Path $p) {
            Remove-Item $p -Recurse -Force
            Write-Step "removed $p"
        }
    }
    Write-Host "`nDone. Restart MicroStation." -ForegroundColor Green
    return
}

if (-not (Test-Path (Join-Path $sourceRoot "data\catalogue.json"))) {
    throw "Source does not contain data\catalogue.json: $sourceRoot"
}

Write-Head "1. Copying tool and library"
$null = New-Item -ItemType Directory -Force -Path $Target
$skip = @("__pycache__", ".lumina_upload_sessions", ".git")
$blocked = @()
foreach ($rel in @(
    "data",
    "schema",
    "tools\build_catalogue.py",
    "tools\microstation",
    "Deploy.bat",
    "README.md",
    "LICENSE",
    "NOTICE"
)) {
    $src = Join-Path $sourceRoot $rel
    if (-not (Test-Path $src)) { continue }
    $dst = Join-Path $Target $rel
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
updated.

Re-run this installer from an elevated prompt:

    Right-click Windows PowerShell > Run as administrator, then:
    powershell -ExecutionPolicy Bypass -File "$PSCommandPath"

"@ -ForegroundColor Yellow
    }
    throw "Install incomplete: $($blocked.Count) file(s) not updated."
}

# Overlay installs preserve files that were removed from the source tree. Clean
# only known obsolete/generated paths so an older deployment cannot leak them
# into the launcher or a shared package.
foreach ($relative in @(
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
    "tools\microstation\ifcsg_checker\librarybuild.py"
)) {
    $stale = Join-Path $Target $relative
    if (Test-Path $stale -PathType Leaf) {
        Remove-Item $stale -Force
        Write-Step "removed obsolete $relative"
    }
}
foreach ($relative in @("data\generated", "data\source", "tools\staad")) {
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

Write-Head "2. Generating ribbon icon"
$iconDir = Join-Path $Target "icons"
$null = New-Item -ItemType Directory -Force -Path $iconDir
$iconPath = Join-Path $iconDir "IFCSG_Checker.ico"
$bmpPath = Join-Path $iconDir "IFCSG_Checker.bmp"
New-ToolIcon $iconPath $bmpPath
Write-Step "wrote $iconPath"
Write-Step "wrote $bmpPath"

Write-Head "3. Writing MicroStation configuration"
$launcher = Join-Path $Target "tools\microstation\run_ifcsg_checker.py"
$cfg = @"
#----------------------------------------------------------------------
# IFCSG_Checker.cfg - IFC+SG / CORENET X pre-flight checker
#
# Dropped into `$(_USTN_ORGANIZATION), which msconfig.cfg includes with
#   %include `$(_USTN_ORGANIZATION)*.cfg
# so these variables are defined for every WorkSet on this machine.
#
# Ribbon button key-in:
#   python load `$(IFCSG_CHECKER_LAUNCHER)
#----------------------------------------------------------------------

IFCSG_CHECKER_DIR       = $Target\
IFCSG_CHECKER_LAUNCHER  = $launcher
IFCSG_CHECKER_ICONS     = $Target\icons\

# Make the checker importable by any MicroStation Python script.
MS_PYTHONPATH           > `$(IFCSG_CHECKER_DIR)tools/microstation/
"@
Set-Content -Path $cfgPath -Value $cfg -Encoding ASCII
Write-Step "wrote $cfgPath"

Write-Head "4. Creating the GUI DGN library"
$null = New-Item -ItemType Directory -Force -Path $guiDir
if (Test-Path $dgnLibPath) {
    Write-Step "already exists, left untouched: $dgnLibPath"
} else {
    $seed = Find-SeedDgnLib
    if ($seed) {
        Copy-Item $seed $dgnLibPath -Force
        Write-Step "created from seed: $dgnLibPath"
    } else {
        Write-Warning "  No seed DGN found. Create the dgnlib manually in $guiDir"
    }
}

Write-Head "Installed."
Write-Host @"

Key-in for the ribbon button (copy this exactly):

    python load $launcher

Remaining step - create the button. This takes TWO dialogs: one defines the tool,
the other places it on the ribbon.

  Restart MicroStation first, so the new configuration is read.

  A. Define the tool     File > Settings > Configuration > Customize
                         (key-in: customize dialog)

     1. File > Open in that dialog, and open
            $dgnLibPath
     2. Tools tab > expand User Tools > the open dgnlib.
        Right-click it > New Toolbox, name it  IFC+SG
        Right-click that toolbox > New Tool,  name it  IFC+SG Checker
     3. With the tool selected, in Properties:
          Command Data > Key-in:
              python load $launcher
          General Settings > Icon: browse to
              $iconPath
     4. File > Save, then close the Customize dialog.

  B. Place it on the ribbon    File > Settings > User > Customize Ribbon
                               (key-in: ribbon customize open close)

     5. 'Choose components from' drop-down > select  Toolbox (Custom)
     6. On the right, expand the workflow > the Content tab > pick a group.
     7. Select your IFC+SG Checker tool, click Add (or drag it across).
     8. Click Apply, then OK.

To share with other users, see share_ifcsg_checker.ps1 in the same folder.

"@ -ForegroundColor Green
